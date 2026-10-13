"""Amazon + TikTok cross-channel fact synthesis (R18).

Facts only: a same-unit (units) comparison table, the evidence IDs behind each
row, and an explicit limitations list. It never emits a business recommendation;
the decision slot is left for a human and the output is scanned with the shared
forbidden-decision patterns before being returned.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from research_workflow.analysis.market import FORBIDDEN_DECISION_PATTERNS

from .evidence import EvidenceLedger
from .forecast import ForecastResult, ScopeLevel
from .tiktok import TikTokAnalysis

HUMAN_DECISION_SLOT = "由人工填写"


@dataclass(frozen=True)
class CrossChannelSummary:
    unit: str
    amazon_scope: str
    tiktok_scope: str
    table: tuple[dict[str, Any], ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    human_decision_slot: str = HUMAN_DECISION_SLOT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assert_no_decision_language(text: str) -> None:
    for pattern in FORBIDDEN_DECISION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise ValueError("Cross-channel summary crossed the human decision boundary")


def _amazon_points(result: ForecastResult) -> dict[str, tuple[float, bool]]:
    points = {result.base_period: (result.base_value, False)}
    for point in result.scenarios.get("mid", ()):
        points[point.period] = (point.value, True)
    return points


def _tiktok_points(analysis: TikTokAnalysis) -> dict[str, tuple[float, bool, tuple[str, ...]]]:
    points: dict[str, tuple[float, bool, tuple[str, ...]]] = {}
    for year, value in analysis.annual:
        points[str(year)] = (value.units, value.is_forecast, value.evidence_ids)
    for month in analysis.monthly_2026:
        points[month.month] = (month.units, month.is_forecast, (month.evidence_id,))
    for point in analysis.projection.points:
        if point.scenario == "mid":
            points[str(point.year)] = (point.units, True, analysis.projection.evidence_ids)
    return points


def build_cross_channel_summary(
    amazon_scope_result: ForecastResult,
    tiktok_result: TikTokAnalysis,
    ledger: EvidenceLedger,
) -> CrossChannelSummary:
    amazon = _amazon_points(amazon_scope_result)
    tiktok = _tiktok_points(tiktok_result)

    table: list[dict[str, Any]] = []
    referenced: list[str] = list(amazon_scope_result.evidence_ids)
    for period in sorted(set(amazon) & set(tiktok)):
        a_units, a_fc = amazon[period]
        t_units, t_fc, t_ids = tiktok[period]
        ids = list(dict.fromkeys([*amazon_scope_result.evidence_ids, *t_ids]))
        referenced.extend(t_ids)
        table.append(
            {
                "period": period,
                "amazon_units": a_units,
                "amazon_is_forecast": a_fc,
                "tiktok_units": t_units,
                "tiktok_is_forecast": t_fc,
                "evidence_ids": ids,
            }
        )
    referenced.extend(tiktok_result.cagr_2024_2026.evidence_ids)
    evidence_ids = tuple(dict.fromkeys(referenced))

    unknown = [e for e in evidence_ids if e not in ledger]
    if unknown:
        raise ValueError(f"evidence ids not in ledger: {unknown}")

    limitations: list[str] = []
    if amazon_scope_result.scope != ScopeLevel.FULL_MARKET or tiktok_result.segment != "total":
        limitations.append(
            f"口径差异:Amazon 侧为 {amazon_scope_result.scope.value}(来源口径 {amazon_scope_result.source_scope.value}),"
            f"TikTok 侧为 {tiktok_result.geo}/{tiktok_result.segment} 的 TikTok 渠道台数;两者统计范围不同,不可直接相加、相减或相除"
        )
    if amazon_scope_result.source_scope == ScopeLevel.TOP_SAMPLE:
        limitations.append("Amazon 侧起点为 Top100 样本销量,经覆盖率假设放大,覆盖率来源见假设清单")
    if tiktok_result.geo != "us":
        limitations.append(f"TikTok 侧地域为 {tiktok_result.geo},并非美国市场")
    forecast_periods = [r["period"] for r in table if r["amazon_is_forecast"] or r["tiktok_is_forecast"]]
    if forecast_periods:
        limitations.append("以下期间含预测值,非实际观测:" + "、".join(forecast_periods))
    if tiktok_result.missing_months_2026:
        limitations.append("TikTok 2026 缺失月份:" + "、".join(tiktok_result.missing_months_2026))
    if tiktok_result.cagr_2024_2026.status != "ok":
        limitations.append("TikTok 2024-2026 CAGR 证据不足,未产出外推结论:" + tiktok_result.cagr_2024_2026.missing_reason)
    unusable = [e.claim_id for e in ledger.missing()]
    if unusable:
        limitations.append("证据账本中来源缺失或不可访问:" + "、".join(unusable))
    if not table:
        limitations.append("两侧无可对齐期间,未生成对比行")

    summary = CrossChannelSummary(
        unit="units",
        amazon_scope=amazon_scope_result.scope.value,
        tiktok_scope=f"{tiktok_result.geo}/{tiktok_result.segment}/tiktok",
        table=tuple(table),
        evidence_ids=evidence_ids,
        limitations=tuple(limitations),
    )
    assert_no_decision_language(json.dumps(summary.to_dict(), ensure_ascii=False, default=str))
    return summary
