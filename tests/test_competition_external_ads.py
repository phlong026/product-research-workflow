from __future__ import annotations

import hashlib
import json

import pytest

from research_workflow.competition_external.ads import (
    AdRecord,
    CpcRecord,
    MonthlyReviewSales,
    ReviewSeries,
    TrafficStructure,
    compute_review_rate_12m,
    load_ad_records,
)
from research_workflow.competition_external.ledger import EvidenceRef, assert_no_decision_language


def _ev():
    return EvidenceRef("Helium10", "2026-10-01", url="https://example.com/h10")


def _months(n=12, reviews=10, sales=100):
    return [MonthlyReviewSales(f"2025-{m:02d}", reviews, sales) for m in range(1, n + 1)]


def test_review_rate_hand_calculation():
    # 12 months: reviews 10 each (120), sales 100..1200 step 100 (7800) -> 120/7800
    months = [MonthlyReviewSales(f"2025-{m:02d}", 10, 100 * m) for m in range(1, 13)]
    result = compute_review_rate_12m(months)
    assert result.new_reviews_total == 120
    assert result.sales_total == 7800
    assert result.rate == pytest.approx(120 / 7800)
    assert result.monthly_rates["2025-01"] == pytest.approx(0.1)
    assert result.note is None


def test_review_rate_zero_denominator_is_none_not_zero():
    result = compute_review_rate_12m(_months(sales=0))
    assert result.rate is None
    assert "分母为0" in result.note
    # a single zero-sales month only voids that month's rate
    months = _months()
    months[0] = MonthlyReviewSales("2025-01", 5, 0)
    result = compute_review_rate_12m(months)
    assert result.monthly_rates["2025-01"] is None
    assert result.rate == pytest.approx(115 / 1100)


def test_review_rate_missing_month_or_value_is_none():
    short = compute_review_rate_12m(_months(n=11))
    assert short.rate is None and "缺月" in short.note
    months = _months()
    months[3] = MonthlyReviewSales("2025-04", None, 100)
    gap = compute_review_rate_12m(months)
    assert gap.rate is None and "缺值" in gap.note and "2025-04" in gap.note


def test_review_rate_rejects_duplicates_and_overlong():
    with pytest.raises(ValueError):
        compute_review_rate_12m(_months() + [MonthlyReviewSales("2025-01", 1, 1)])
    dup = _months(n=3) + [MonthlyReviewSales("2025-01", 1, 1)]
    with pytest.raises(ValueError, match="duplicate"):
        compute_review_rate_12m(dup)


def test_blocks_require_time_range():
    with pytest.raises(ValueError, match="time_range"):
        TrafficStructure("", _ev(), {"organic": 0.6})
    with pytest.raises(ValueError, match="time_range"):
        ReviewSeries(" ", _ev())
    with pytest.raises(ValueError, match="time_range"):
        CpcRecord("", _ev(), 1.2)


def test_cpc_screenshot_needs_sha256_and_matches_file(tmp_path):
    shot = tmp_path / "cpc.png"
    shot.write_bytes(b"png-bytes")
    digest = hashlib.sha256(b"png-bytes").hexdigest()
    with pytest.raises(ValueError, match="sha256"):
        CpcRecord("2026-07..2026-09", EvidenceRef("screenshot", "2026-10-01", file_path=str(shot)), 1.5)
    good = CpcRecord(
        "2026-07..2026-09",
        EvidenceRef("screenshot", "2026-10-01", file_path=str(shot), sha256=digest),
        1.5,
        "USD",
    )
    assert good.to_dict()["evidence"]["file_check"] == "match"
    with pytest.raises(ValueError, match="mismatch"):
        EvidenceRef("screenshot", "2026-10-01", file_path=str(shot), sha256="f" * 64)


def test_ad_record_requires_human_selector_and_known_cohort():
    with pytest.raises(ValueError, match="selected_by"):
        AdRecord("B0AAA", "head_waist_tail", "")
    with pytest.raises(ValueError, match="cohort"):
        AdRecord("B0AAA", "random", "analyst")
    for cohort in ("head_waist_tail", "new_product"):
        AdRecord("B0AAA", cohort, "analyst")


def test_to_dict_notes_and_no_verdict_words():
    record = AdRecord(
        "B0AAA",
        "new_product",
        "analyst",
        traffic=TrafficStructure("2026-07..2026-09", _ev(), {"organic_share": "55%", "ad_share": "45%"}),
        reviews=ReviewSeries("2025-10..2026-09", _ev(), tuple(_months(n=10))),
    )
    out = record.to_dict()
    assert out["traffic"]["data"] == {"organic_share": "55%", "ad_share": "45%"}  # kept verbatim
    assert out["reviews"]["review_rate_12m"]["rate"] is None
    assert any("未登记: cpc" in note for note in out["notes"])
    assert any("缺月" in note for note in out["notes"])
    assert_no_decision_language(out["notes"])


def test_load_csv_with_block_source_override_and_missing_source(tmp_path):
    months = json.dumps([{"month": f"2025-{m:02d}", "new_reviews": 10, "sales": 100} for m in range(1, 13)])
    path = tmp_path / "ads.csv"
    import csv

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["asin", "cohort", "selected_by", "source_name", "url", "retrieved_at",
             "reviews_time_range", "review_months", "cpc_time_range", "cpc_value", "cpc_currency",
             "cpc_source_name", "cpc_url", "cpc_retrieved_at"]
        )
        writer.writerow(
            ["B0AAA", "head_waist_tail", "analyst", "Helium10", "https://h10/x", "2026-10-01",
             "2025-10..2026-09", months, "2026-07..2026-09", "1.35", "USD",
             "Helium10 Adds", "https://h10/cpc", "2026-10-02"]
        )
    record = load_ad_records(path)[0]
    assert record.reviews.to_dict()["review_rate_12m"]["rate"] == pytest.approx(0.1)
    assert record.cpc.value == 1.35
    assert record.cpc.evidence.source_name == "Helium10 Adds"
    assert record.traffic is None

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps([{"asin": "B0AAA", "cohort": "new_product", "selected_by": "a",
                     "cpc_time_range": "2026-07..2026-09", "cpc_value": 1.0}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source_name"):
        load_ad_records(bad)
