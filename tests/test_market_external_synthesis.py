import json
import re

import pytest

from research_workflow.analysis.market import FORBIDDEN_DECISION_PATTERNS as MARKET_PATTERNS
from research_workflow.market_external import (
    CoverageAssumption,
    EvidenceLedger,
    ExternalEvidence,
    ScopeLevel,
    SeriesPoint,
    analyze_tiktok,
    extrapolate,
    parse_units_rows,
)
from research_workflow.market_external.synthesis import (
    HUMAN_DECISION_SLOT,
    assert_no_decision_language,
    build_cross_channel_summary,
)


def _tiktok(geo="us"):
    def row(period, units, channel="tiktok", fc=False):
        return {"geo": geo, "segment": "countertop", "channel": channel, "period": period,
                "units": units, "is_forecast": fc, "evidence_id": f"tt:{channel}:{period}"}
    rows = [row("2024", 100), row("2026", 144, fc=True), row("2024", 1000, "all"), row("2026", 1200, "all", True)]
    return analyze_tiktok(parse_units_rows(rows), geo=geo, segment="countertop", high_growth=0.3, low_growth=0.1)


def _amazon(evidence=("am:1",)):
    series = [SeriesPoint("2024", 400.0), SeriesPoint("2025", 440.0), SeriesPoint("2026", 484.0)]
    return extrapolate(series, ScopeLevel.AMAZON_CHANNEL, horizon_years=2, evidence_ids=evidence)


def _ledger(extra=()):
    ledger = EvidenceLedger()
    ids = ["am:1", "tt:tiktok:2024", "tt:tiktok:2026", "tt:all:2024", "tt:all:2026"]
    for i in ids:
        ledger.add(ExternalEvidence(i, "units", 1.0, "units", "p", "vendor", "file://v.csv", "2026-01-01", "file"))
    for e in extra:
        ledger.add(e)
    return ledger


def test_summary_table_same_unit_with_flags_and_evidence():
    s = build_cross_channel_summary(_amazon(), _tiktok(), _ledger())
    rows = {r["period"]: r for r in s.table}
    assert s.unit == "units" and set(rows) == {"2026", "2027", "2028"}
    assert rows["2026"]["amazon_units"] == 484.0 and rows["2026"]["amazon_is_forecast"] is False
    assert rows["2026"]["tiktok_units"] == 144.0 and rows["2026"]["tiktok_is_forecast"] is True
    assert rows["2027"]["tiktok_units"] == pytest.approx(172.8) and rows["2027"]["amazon_is_forecast"] is True
    assert "am:1" in rows["2027"]["evidence_ids"] and "tt:tiktok:2024" in rows["2027"]["evidence_ids"]
    assert set(s.evidence_ids) >= {"am:1", "tt:tiktok:2026", "tt:tiktok:2024"}


def test_limitations_cover_scope_forecast_and_missing_sources():
    missing = ExternalEvidence("rep:1", "report_excerpt", None, "text", "2025", "Paywalled", "https://x", "", "manual",
                               status="inaccessible")
    s = build_cross_channel_summary(_amazon(), _tiktok(), _ledger([missing]))
    text = "\n".join(s.limitations)
    assert "口径差异" in text and "预测" in text and "2027" in text
    assert "rep:1" in text and "2026-01" in text  # inaccessible source + missing TikTok months
    assert s.human_decision_slot == HUMAN_DECISION_SLOT == "由人工填写"


def test_global_tiktok_and_sample_scope_flagged():
    cov = CoverageAssumption(0.5, "vendor estimate", ScopeLevel.TOP_SAMPLE, ScopeLevel.AMAZON_CHANNEL)
    amazon = extrapolate([SeriesPoint("2025", 200.0), SeriesPoint("2026", 242.0)], ScopeLevel.TOP_SAMPLE,
                         horizon_years=1, target_scope=ScopeLevel.AMAZON_CHANNEL, coverage=cov,
                         evidence_ids=["am:1"])
    s = build_cross_channel_summary(amazon, _tiktok("global"), _ledger())
    text = "\n".join(s.limitations)
    assert "Top100 样本" in text and "global" in text


def test_unknown_evidence_id_rejected():
    with pytest.raises(ValueError, match="not in ledger"):
        build_cross_channel_summary(_amazon(("nope:1",)), _tiktok(), _ledger())


def test_output_has_no_decision_language():
    s = build_cross_channel_summary(_amazon(), _tiktok(), _ledger())
    blob = json.dumps(s.to_dict(), ensure_ascii=False, default=str)
    for pattern in MARKET_PATTERNS:
        assert not re.search(pattern, blob, flags=re.IGNORECASE), pattern
    assert_no_decision_language(blob)


@pytest.mark.parametrize("text", ["建议进入美国市场", "YES", "最终决策:做", "go/no-go"])
def test_guard_rejects_decision_language(text):
    with pytest.raises(ValueError, match="decision boundary"):
        assert_no_decision_language(text)
