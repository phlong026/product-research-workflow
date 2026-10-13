"""基于 Claude 的商品分类 Provider（只做分类，不做 Yes/No 判断）。

依赖 `anthropic` 可选包（`pip install -e '.[llm]'`），密钥只读环境变量 ANTHROPIC_API_KEY（红线 3）。
分批调用；单批失败/被拒绝/输出非法时该批商品不返回，由 ClassificationService
统一落为 unknown+low 供人工处理，不会静默丢失。累计 token 用量写入 `usage`，用于核算费用。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from research_workflow.connectors.base import ConnectorError, with_retries
from research_workflow.enrichment.models import ASINProfile

from .models import ClassificationSchema
from .prompt import build_classification_prompt

DEFAULT_MODEL = "claude-opus-5-5"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(slots=True)
class LLMUsage:
    requests: int = 0
    failed_batches: int = 0
    refusals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed_asins: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class AnthropicClassificationProvider:
    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
        batch_size: int = 25,
        effort: str = "medium",
        max_tokens: int = 8000,
        timeout_seconds: float = 300.0,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.model = model or os.environ.get("RESEARCH_LLM_MODEL") or DEFAULT_MODEL
        self.name = f"anthropic:{self.model}"
        self.batch_size = batch_size
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.usage = LLMUsage()
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency boundary
                raise RuntimeError("需要安装 llm 可选依赖: pip install -e '.[llm]'") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("未设置 ANTHROPIC_API_KEY；密钥只能通过环境变量注入")
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def classify(
        self, schema: ClassificationSchema, profiles: list[ASINProfile]
    ) -> list[dict[str, Any]]:
        ordered = sorted(profiles, key=lambda p: p.asin)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ordered), self.batch_size):
            chunk = ordered[start : start + self.batch_size]
            parsed = await self._classify_chunk(schema, chunk)
            if parsed is None:
                self.usage.failed_batches += 1
                self.usage.failed_asins.extend(p.asin for p in chunk)
                continue
            allowed = {p.asin.upper() for p in chunk}
            rows.extend(r for r in parsed if str(r.get("asin", "")).upper() in allowed)
        return rows

    async def _classify_chunk(
        self, schema: ClassificationSchema, chunk: list[ASINProfile]
    ) -> list[dict[str, Any]] | None:
        client = self._get_client()
        prompt = build_classification_prompt(schema, chunk)
        for _attempt in range(2):
            try:
                response = await with_retries(
                    lambda: client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        output_config={"effort": self.effort},
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    attempts=self.retry_attempts,
                    base_delay_seconds=self.retry_delay_seconds,
                    timeout_seconds=self.timeout_seconds,
                )
            except ConnectorError as exc:  # 超时/限流/网络：该批降级为 unknown，交人工
                self.usage.errors.append(str(exc))
                return None
            self.usage.requests += 1
            usage = getattr(response, "usage", None)
            self.usage.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.usage.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            if getattr(response, "stop_reason", None) == "refusal":
                self.usage.refusals += 1
                return None  # 被拒绝的批次不重试，交人工分类
            text = "".join(
                getattr(block, "text", "") for block in getattr(response, "content", [])
                if getattr(block, "type", "") == "text"
            )
            parsed = _parse_rows(text)
            if parsed is not None:
                return parsed
        return None


def _parse_rows(text: str) -> list[dict[str, Any]] | None:
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except ValueError:
            return None
    if isinstance(data, dict):
        data = data.get("rows") or data.get("classifications")
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]
