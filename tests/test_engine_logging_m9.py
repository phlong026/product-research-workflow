"""引擎核心日志(M9)与月度抓取失败类别(M7 库侧)。"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from research_workflow.classification import (
    ClassificationLabel,
    ClassificationSchema,
    LLMClassificationProvider,
)
from research_workflow.connectors import ConnectorError, SellerSpriteConnector
from research_workflow.connectors.base import with_retries
from research_workflow.discovery import fetch_monthly_top
from research_workflow.enrichment import (
    ASINEnrichmentService,
    ASINProfile,
    enrich_bullets_unique_asins,
    enrich_missing_regions,
)

TOKEN = "sk-abcdef1234567890abcdef"
SECRET_ENV = "SECRET123"
LEAKY = f"401 Unauthorized Bearer {TOKEN} api_key={SECRET_ENV}"


def assert_clean(caplog):
    text = caplog.text
    assert TOKEN not in text
    assert SECRET_ENV not in text


def warnings_of(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---- with_retries ---------------------------------------------------------


def test_with_retries_logs_each_retry_and_final_failure(caplog):
    caplog.set_level(logging.DEBUG)

    async def boom():
        raise RuntimeError(LEAKY)

    with pytest.raises(ConnectorError) as info:
        asyncio.run(
            with_retries(boom, attempts=3, base_delay_seconds=0, operation="product_research")
        )
    # 对外异常仍然脱敏:只剩类型名
    assert LEAKY not in str(info.value) and "RuntimeError" in str(info.value)
    records = warnings_of(caplog)
    assert len(records) == 3  # 2 次重试 + 1 次最终失败
    assert all("product_research" in r.getMessage() for r in records)
    assert "RuntimeError" in caplog.text
    assert "401 Unauthorized" in caplog.text  # 日志保留(脱敏后的)消息
    assert_clean(caplog)


def test_with_retries_truncates_long_messages(caplog):
    async def boom():
        raise RuntimeError("x " * 5000)

    with pytest.raises(ConnectorError):
        asyncio.run(with_retries(boom, attempts=1, operation="op"))
    assert max(len(r.getMessage()) for r in warnings_of(caplog)) < 600


def test_with_retries_success_does_not_warn(caplog):
    async def ok():
        return 1

    assert asyncio.run(with_retries(ok, operation="op")) == 1
    assert warnings_of(caplog) == []


# ---- monthly_fetch ----------------------------------------------------------


class MonthGateway:
    def __init__(self, empty=(), failing=()):
        self.empty = set(empty)
        self.failing = set(failing)

    async def list_tools(self, server_name):
        return []

    async def call_tool(self, server_name, tool_name, arguments=None):
        params = (arguments or {})["params"]
        month = params["month"]
        if month in self.failing:
            raise RuntimeError(LEAKY)
        if month in self.empty:
            return {"structured_content": {"items": [], "total": 0}}
        items = [{"asin": f"{month}-{i}", "amz_unit": 100 - i} for i in range(3)]
        return {"structured_content": {"items": items, "total": 3}}


def fetch(gateway, months):
    conn = SellerSpriteConnector(gateway, retry_attempts=1)
    return asyncio.run(
        fetch_monthly_top(conn, marketplace="us", keyword="kw", months=months, top_n=3, max_pages=1)
    )


def test_missing_month_logged_with_month_and_reason_category(caplog):
    result = fetch(MonthGateway(empty=["202602"], failing=["202603"]), ["2026-01", "2026-02", "2026-03"])
    messages = [r.getMessage() for r in warnings_of(caplog) if "2026-0" in r.getMessage()]
    assert any("2026-02" in m and "empty_response" in m for m in messages)
    assert any("2026-03" in m and "connector_error" in m for m in messages)
    assert not any("2026-01" in m for m in messages)
    assert_clean(caplog)
    assert result.months == ("2026-01", "2026-02", "2026-03")


def test_connector_error_and_empty_months_are_distinguished():
    result = fetch(
        MonthGateway(empty=["202602", "202605"], failing=["202604", "202603"]),
        ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"],
    )
    assert result.connector_error_months() == ["2026-03", "2026-04"]
    assert result.empty_months() == ["2026-02", "2026-05"]


def test_no_missing_months_gives_empty_lists():
    result = fetch(MonthGateway(), ["2026-01"])
    assert result.connector_error_months() == [] and result.empty_months() == []


# ---- backfill --------------------------------------------------------------


class FakeProvider:
    def __init__(self, name, payloads):
        self.name = name
        self.payloads = payloads

    async def get_product_detail(self, asin, marketplace):
        value = self.payloads.get(asin)
        if isinstance(value, Exception):
            raise value
        return value


def test_region_backfill_failure_and_conflict_are_logged(caplog):
    primary = FakeProvider(
        "primary",
        {"A1": RuntimeError(LEAKY), "A2": {"asin": "A2", "sellerRegion": "CN"}},
    )
    fallback = FakeProvider("fallback", {"A1": {"asin": "A1", "sellerRegion": "US"}, "A2": {"asin": "A2", "sellerRegion": "US"}})
    svc = ASINEnrichmentService(primary, [fallback])
    profiles = [ASINProfile(asin="A1", title="t"), ASINProfile(asin="A2", title="t")]
    asyncio.run(enrich_missing_regions(profiles, svc, attempts=1))
    text = [r.getMessage() for r in warnings_of(caplog)]
    assert any("A1" in m and "primary" in m for m in text)  # 补齐失败
    assert any("A2" in m and "seller_region" in m for m in text)  # 冲突
    assert_clean(caplog)


def test_bullet_backfill_failure_and_conflict_are_logged(caplog):
    primary = FakeProvider(
        "primary",
        {"B1": RuntimeError(LEAKY), "B2": {"asin": "B2", "title": "t", "bullets": ["new bullet"]}},
    )
    svc = ASINEnrichmentService(primary, [])
    records = {"2026-01": [{"asin": "B1"}, {"asin": "B2", "bullets": ["old bullet"]}, {"asin": "B3", "bullets": ["x"]}]}
    # B2 已有 bullets,不会被抓取;让 B2 缺失 bullets 在另一月份触发抓取以制造冲突
    records["2026-02"] = [{"asin": "B2"}]
    asyncio.run(enrich_bullets_unique_asins(records, svc, attempts=1))
    text = [r.getMessage() for r in warnings_of(caplog)]
    assert any("B1" in m for m in text)
    assert any("B2" in m and "bullets" in m for m in text)
    assert_clean(caplog)


# ---- llm_provider -----------------------------------------------------------

SCHEMA = ClassificationSchema(
    project_id="p1",
    dimension="shape",
    labels=[ClassificationLabel("round", "圆形", "round"), ClassificationLabel("square", "方形", "square")],
)


def test_llm_chunk_failure_logged_without_prompt_or_product_text(caplog):
    prompts = []

    async def complete(prompt):
        prompts.append(prompt)
        raise RuntimeError(LEAKY)

    profiles = [ASINProfile(asin="T1", title="UNIQUE-TITLE-XYZ", bullets=["UNIQUE-BULLET-XYZ"])]
    provider = LLMClassificationProvider(complete, attempts=1)
    asyncio.run(provider.classify(SCHEMA, profiles))
    assert prompts  # 调用确实发生
    messages = " ".join(r.getMessage() for r in warnings_of(caplog))
    assert "llm" in messages.lower()
    assert "UNIQUE-TITLE-XYZ" not in caplog.text and "UNIQUE-BULLET-XYZ" not in caplog.text
    assert_clean(caplog)
    assert json.dumps(prompts[0])  # 保证 prompt 本身未被日志替换
