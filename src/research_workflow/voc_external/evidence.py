from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from research_workflow.competition_external.ledger import (
    assert_no_decision_language,
    clean_text,
    optional_float,
    read_rows,
    require_text,
)

SENTIMENTS = ("positive", "negative", "neutral")
_SENTIMENT_ALIASES = {
    "positive": "positive", "正面": "positive", "好评": "positive",
    "negative": "negative", "负面": "negative", "差评": "negative",
    "neutral": "neutral", "中性": "neutral",
}
EXAMPLES_PER_THEME = 3
_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

NOTES = (
    "主题、情感、场景、细分标签均来自导入文件(人工或外部 AI 标注),系统只做计数与占比,不做语义判断",
    "缺失的搜索量/提及数不当 0:不进份额分母,并在「缺值行数」中如实列出",
)
assert_no_decision_language(NOTES)


@dataclass(frozen=True, slots=True)
class KeywordRow:
    keyword: str
    search_volume: float | None = None
    aba_rank: int | None = None
    month: str | None = None
    segment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    scenario: str
    mention_count: float | None = None
    segment: str | None = None
    example: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VocRow:
    theme: str
    sentiment: str
    excerpt: str
    platform: str | None = None
    asin: str | None = None
    rating: float | None = None
    locator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _month(value: Any, row_no: int) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    text = text[:7]
    if not _MONTH.fullmatch(text):
        raise ValueError(f"row {row_no}: month 必须是 YYYY-MM: {value!r}")
    return text


def _non_negative(value: Any, name: str, row_no: int) -> float | None:
    number = optional_float(value)
    if clean_text(value) is not None and number is None:
        raise ValueError(f"row {row_no}: {name} 不是数字: {value!r}")
    if number is not None and number < 0:
        raise ValueError(f"row {row_no}: {name} 不能为负: {value!r}")
    return number


def load_keyword_rows(path: str | Path) -> list[KeywordRow]:
    rows: list[KeywordRow] = []
    seen: set[tuple[str, str | None]] = set()
    for index, raw in enumerate(read_rows(path), start=2):
        keyword = require_text(_get(raw, "keyword", "关键词"), f"row {index}: keyword")
        month = _month(_get(raw, "month", "月份"), index)
        key = (keyword.lower(), month)
        if key in seen:
            raise ValueError(f"row {index}: 重复的 (keyword, month): {keyword}/{month}")
        seen.add(key)
        rank = _non_negative(_get(raw, "aba_rank", "ABA排名"), "aba_rank", index)
        rows.append(KeywordRow(
            keyword, _non_negative(_get(raw, "search_volume", "搜索量"), "search_volume", index),
            int(rank) if rank is not None else None, month, clean_text(_get(raw, "segment", "细分")),
        ))
    return rows


def load_scenario_rows(path: str | Path) -> list[ScenarioRow]:
    rows: list[ScenarioRow] = []
    for index, raw in enumerate(read_rows(path), start=2):
        scenario = require_text(_get(raw, "scenario", "场景"), f"row {index}: scenario")
        rows.append(ScenarioRow(
            scenario, _non_negative(_get(raw, "mention_count", "提及数"), "mention_count", index),
            clean_text(_get(raw, "segment", "细分")), clean_text(_get(raw, "example", "示例")),
        ))
    return rows


def load_voc_rows(path: str | Path) -> list[VocRow]:
    rows: list[VocRow] = []
    for index, raw in enumerate(read_rows(path), start=2):
        theme = require_text(_get(raw, "theme", "主题"), f"row {index}: theme")
        sentiment_raw = require_text(_get(raw, "sentiment", "情感"), f"row {index}: sentiment")
        sentiment = _SENTIMENT_ALIASES.get(sentiment_raw.lower()) or _SENTIMENT_ALIASES.get(sentiment_raw)
        if sentiment is None:
            raise ValueError(f"row {index}: sentiment 只能是 {list(SENTIMENTS)}(或 正面/负面/中性): {sentiment_raw!r}")
        excerpt = require_text(_get(raw, "excerpt", "原文", "text"), f"row {index}: excerpt")
        rating = _non_negative(_get(raw, "rating", "评分"), "rating", index)
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError(f"row {index}: rating 必须在 1-5: {rating}")
        rows.append(VocRow(
            theme, sentiment, excerpt, clean_text(_get(raw, "platform", "平台")),
            (clean_text(_get(raw, "asin", "ASIN")) or "").upper() or None, rating,
            clean_text(_get(raw, "url", "review_id", "链接")),
        ))
    return rows


def _share(part: float, total: float) -> float | None:
    return round(part / total * 100, 2) if total else None


def summarize_keywords(rows: list[KeywordRow]) -> dict[str, Any]:
    """按关键词取最新月份的搜索量;有两个及以上月份的给出最近两期变化。缺搜索量不当 0。"""
    by_kw: dict[str, list[KeywordRow]] = defaultdict(list)
    for row in rows:
        by_kw[row.keyword.lower()].append(row)
    table = []
    for series in by_kw.values():
        series.sort(key=lambda r: (r.month or ""))
        observed = [r for r in series if r.search_volume is not None]
        latest = observed[-1] if observed else None
        previous = observed[-2] if len(observed) >= 2 else None
        change = None
        if latest and previous and previous.search_volume:
            change = round((latest.search_volume - previous.search_volume) / previous.search_volume * 100, 2)
        table.append({
            "keyword": series[0].keyword, "segment": next((r.segment for r in series if r.segment), None),
            "latest_month": latest.month if latest else None,
            "latest_search_volume": latest.search_volume if latest else None,
            "previous_month": previous.month if previous else None,
            "previous_search_volume": previous.search_volume if previous else None,
            "change_pct": change, "months_observed": len(observed),
            "aba_rank_latest": next((r.aba_rank for r in reversed(series) if r.aba_rank is not None), None),
        })
    table.sort(key=lambda r: (r["latest_search_volume"] is None, -(r["latest_search_volume"] or 0), r["keyword"].lower()))
    volume_total = sum(r["latest_search_volume"] for r in table if r["latest_search_volume"] is not None)
    segments: dict[str, float] = defaultdict(float)
    for r in table:
        if r["latest_search_volume"] is not None:
            segments[r["segment"] or "未标注"] += r["latest_search_volume"]
    return {
        "keyword_count": len(table), "missing_volume_keywords": sum(1 for r in table if r["latest_search_volume"] is None),
        "table": table,
        "segment_volume": [
            {"segment": name, "search_volume": value, "share_pct": _share(value, volume_total)}
            for name, value in sorted(segments.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "notes": list(NOTES),
    }


def summarize_scenarios(rows: list[ScenarioRow]) -> dict[str, Any]:
    by_scenario: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_scenario.setdefault(row.scenario.strip().lower(), {
            "scenario": row.scenario.strip(), "mentions": 0.0, "rows": 0, "missing_count_rows": 0, "segments": set(), "example": None,
        })
        item["rows"] += 1
        if row.mention_count is None:
            item["missing_count_rows"] += 1
        else:
            item["mentions"] += row.mention_count
        if row.segment:
            item["segments"].add(row.segment)
        item["example"] = item["example"] or row.example
    total = sum(item["mentions"] for item in by_scenario.values())
    table = [
        {"scenario": item["scenario"], "mentions": item["mentions"], "share_pct": _share(item["mentions"], total),
         "rows": item["rows"], "missing_count_rows": item["missing_count_rows"],
         "segments": sorted(item["segments"]), "example": item["example"]}
        for item in by_scenario.values()
    ]
    table.sort(key=lambda r: (-r["mentions"], r["scenario"].lower()))
    return {"scenario_count": len(table), "mentions_total": total, "table": table, "notes": list(NOTES)}


def summarize_voc(rows: list[VocRow]) -> dict[str, Any]:
    themes: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = themes.setdefault(row.theme.strip().lower(), {
            "theme": row.theme.strip(), "excerpts": 0, "positive": 0, "negative": 0, "neutral": 0, "examples": [],
        })
        item["excerpts"] += 1
        item[row.sentiment] += 1
        if len(item["examples"]) < EXAMPLES_PER_THEME:
            item["examples"].append({"sentiment": row.sentiment, "excerpt": row.excerpt, "platform": row.platform,
                                     "asin": row.asin, "locator": row.locator})
    total = len(rows)
    table = [
        {**{k: v for k, v in item.items() if k != "examples"}, "share_pct": _share(item["excerpts"], total),
         "negative_share_pct": _share(item["negative"], item["excerpts"]), "examples": item["examples"]}
        for item in themes.values()
    ]
    table.sort(key=lambda r: (-r["excerpts"], r["theme"].lower()))
    sentiments = {s: sum(1 for r in rows if r.sentiment == s) for s in SENTIMENTS}
    return {
        "excerpt_count": total, "theme_count": len(table), "sentiment_counts": sentiments, "table": table,
        "notes": [*NOTES, "摘录条数不等于评论总体:样本范围见导入时登记的 scope"],
    }
