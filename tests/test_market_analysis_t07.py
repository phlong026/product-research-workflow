from __future__ import annotations

import asyncio

import pytest

from research_workflow.analysis import (
    CallbackMarketResearchProvider,
    MarketResearchService,
    build_evidence_index,
    build_market_research_prompt,
)
from research_workflow.data.models import BSRRecord
from research_workflow.metrics import build_market_metrics


def metrics_fixture():
    rows = [
        BSRRecord("N", "2025-01", 20),
        BSRRecord("B", "2025-01", 80),
        BSRRecord("N", "2026-01", 25),
        BSRRecord("B", "2026-01", 55),
    ]
    return build_market_metrics(
        rows,
        {"N": "nugget", "B": "bullet"},
        decomposition_pairs=[("2025-01", "2026-01")],
    )


def test_deterministic_market_research_detects_segment_divergence_without_decision():
    report = asyncio.run(MarketResearchService().analyze(metrics_fixture()))
    assert any("细分与大盘走势背离" == f.title for f in report.findings)
    assert any("产品替代" in q for q in report.follow_up_questions)
    rendered = str(report.to_dict())
    assert "建议进入" not in rendered
    assert "不建议进入" not in rendered


def test_evidence_index_has_stable_metric_ids_and_prompt_requires_citations():
    metrics = metrics_fixture()
    evidence = build_evidence_index(metrics)
    assert "market:2026-01" in evidence
    assert "segment:2026-01:nugget" in evidence
    assert "decomp:2025-01:2026-01:nugget" in evidence
    prompt = build_market_research_prompt(metrics)
    assert "Every finding must cite" in prompt
    assert "Do not output a final YES/NO" in prompt


def test_llm_finding_with_fake_evidence_is_dropped():
    provider = CallbackMarketResearchProvider(
        lambda prompt: {
            "summary": "Summary from supplied metrics.",
            "findings": [
                {
                    "key": "good",
                    "title": "Validated",
                    "detail": "A cited fact",
                    "evidence_ids": ["market:2026-01"],
                    "severity": "info",
                },
                {
                    "key": "fake",
                    "title": "Hallucinated",
                    "detail": "No real evidence",
                    "evidence_ids": ["made-up:metric"],
                    "severity": "anomaly",
                },
            ],
            "follow_up_questions": [],
        },
        name="fake-llm",
    )
    report = asyncio.run(MarketResearchService(provider).analyze(metrics_fixture()))
    assert [f.key for f in report.findings] == ["good"]
    assert report.provider == "fake-llm"


def test_llm_output_cannot_cross_final_decision_boundary():
    provider = CallbackMarketResearchProvider(
        lambda prompt: {
            "summary": "建议进入这个市场",
            "findings": [],
            "follow_up_questions": [],
        }
    )
    with pytest.raises(ValueError, match="human decision boundary"):
        asyncio.run(MarketResearchService(provider).analyze(metrics_fixture()))
