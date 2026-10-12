from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Protocol

from research_workflow.metrics.models import MarketMetricsResult

from .models import MarketResearchReport, ResearchFinding


FORBIDDEN_DECISION_PATTERNS = (
    r"\bYES\b",
    r"\bNO\b",
    r"建议进入",
    r"不建议进入",
    r"最终决策",
    r"go/no-go",
)


class MarketResearchProvider(Protocol):
    name: str

    async def analyze(self, prompt: str) -> dict[str, Any] | str: ...


class CallbackMarketResearchProvider:
    def __init__(self, callback, *, name: str = "llm") -> None:
        self.callback = callback
        self.name = name

    async def analyze(self, prompt: str) -> dict[str, Any] | str:
        result = self.callback(prompt)
        if hasattr(result, "__await__"):
            result = await result
        return result


def build_evidence_index(metrics: MarketMetricsResult) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for row in metrics.market:
        evidence[f"market:{row.month}"] = asdict(row)
    for row in metrics.segments:
        evidence[f"segment:{row.month}:{row.segment}"] = asdict(row)
    for row in metrics.seller_regions:
        evidence[f"seller:{row.month}:{row.seller_region}"] = asdict(row)
    for row in metrics.decompositions:
        evidence[f"decomp:{row.base_month}:{row.current_month}:{row.segment}"] = asdict(row)
    return evidence


def build_market_research_prompt(metrics: MarketMetricsResult) -> str:
    evidence = build_evidence_index(metrics)
    return (
        "You are a market research analyst. Your job is to explain already-computed facts, "
        "not to recalculate them and not to decide whether the product should be launched.\n"
        "Rules:\n"
        "1. Use ONLY the evidence payload below.\n"
        "2. Every finding must cite one or more exact evidence_ids.\n"
        "3. Do not output a final YES/NO, score, or enter/avoid recommendation.\n"
        "4. If a causal explanation is not proven by the supplied metrics, phrase it as a follow-up question, not as a fact.\n"
        "5. Return JSON only with: summary, findings[], follow_up_questions[].\n"
        "Each finding: key,title,detail,evidence_ids,severity(info|watch|anomaly).\n\n"
        f"Evidence: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
    )


class MarketResearchService:
    def __init__(self, provider: MarketResearchProvider | None = None) -> None:
        self.provider = provider

    async def analyze(self, metrics: MarketMetricsResult) -> MarketResearchReport:
        evidence = build_evidence_index(metrics)
        deterministic = _deterministic_report(metrics)
        if self.provider is None:
            return deterministic

        raw = await self.provider.analyze(build_market_research_prompt(metrics))
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError("market research provider must return an object")

        report = _parse_llm_report(raw, evidence, provider=self.provider.name)
        _assert_no_business_decision(report)
        return report


def _deterministic_report(metrics: MarketMetricsResult) -> MarketResearchReport:
    evidence = build_evidence_index(metrics)
    findings: list[ResearchFinding] = []
    questions: list[str] = []

    if metrics.market:
        latest = sorted(metrics.market, key=lambda x: x.month)[-1]
        if latest.yoy_pct is not None:
            direction = "增长" if latest.yoy_pct > 0 else "下降" if latest.yoy_pct < 0 else "持平"
            findings.append(
                ResearchFinding(
                    key="latest_market_yoy",
                    title="最新市场同比",
                    detail=f"{latest.month} 市场销量同比{direction} {abs(latest.yoy_pct):.2f}% 。",
                    evidence_ids=(f"market:{latest.month}",),
                    severity="watch" if latest.yoy_pct < 0 else "info",
                )
            )

    for row in metrics.decompositions:
        if (
            row.market_change_pct is not None
            and row.segment_change_pct is not None
            and row.market_change_pct < 0 < row.segment_change_pct
        ):
            evidence_id = f"decomp:{row.base_month}:{row.current_month}:{row.segment}"
            findings.append(
                ResearchFinding(
                    key=f"segment_divergence:{row.segment}:{row.current_month}",
                    title="细分与大盘走势背离",
                    detail=(
                        f"{row.segment} 在 {row.current_month} 相比 {row.base_month} 销量增长 "
                        f"{row.segment_change_pct:.2f}%，同期大盘下降 {abs(row.market_change_pct):.2f}%，"
                        f"份额变化 {row.share_change_pp:+.2f} 个百分点。"
                    ),
                    evidence_ids=(evidence_id,),
                    severity="anomaly",
                )
            )
            questions.append(
                f"需要进一步研究 {row.segment} 逆大盘变化的原因：产品替代、渠道迁移、季节性还是新品推动？"
            )

    if not findings:
        summary = "已完成市场指标整理；当前指标未触发内置异常摘要。"
    else:
        summary = f"已基于 {len(evidence)} 条结构化指标生成 {len(findings)} 条研究发现。"
    report = MarketResearchReport(
        summary=summary,
        findings=findings,
        follow_up_questions=list(dict.fromkeys(questions)),
        provider="deterministic",
        metadata={"evidence_count": len(evidence)},
    )
    _assert_no_business_decision(report)
    return report


def _parse_llm_report(
    raw: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    *,
    provider: str,
) -> MarketResearchReport:
    findings: list[ResearchFinding] = []
    for idx, item in enumerate(raw.get("findings", [])):
        if not isinstance(item, dict):
            continue
        refs = tuple(str(x) for x in item.get("evidence_ids", []) if str(x) in evidence)
        if not refs:
            # A finding without verified evidence is not allowed into the report.
            continue
        severity = str(item.get("severity", "info")).lower()
        if severity not in {"info", "watch", "anomaly"}:
            severity = "info"
        findings.append(
            ResearchFinding(
                key=str(item.get("key") or f"finding_{idx+1}"),
                title=str(item.get("title", "")).strip(),
                detail=str(item.get("detail", "")).strip(),
                evidence_ids=refs,
                severity=severity,
            )
        )

    questions = [
        str(q).strip() for q in raw.get("follow_up_questions", []) if str(q).strip()
    ]
    return MarketResearchReport(
        summary=str(raw.get("summary", "")).strip(),
        findings=findings,
        follow_up_questions=questions,
        provider=provider,
        metadata={"evidence_count": len(evidence)},
    )


def _assert_no_business_decision(report: MarketResearchReport) -> None:
    text = "\n".join(
        [
            report.summary,
            *[f"{row.title}\n{row.detail}" for row in report.findings],
            *report.follow_up_questions,
        ]
    )
    for pattern in FORBIDDEN_DECISION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            raise ValueError("Market research output crossed the human decision boundary")
