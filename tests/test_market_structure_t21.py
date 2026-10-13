"""T2-1 region x segment cross-tab and T2-2 region MoM/YoY + seasonality (synthetic data)."""
from __future__ import annotations

import json
import math

from research_workflow.data.models import BSRRecord
from research_workflow.metrics import build_market_metrics, detect_seasonality


def rec(asin, month, sales, region=""):
    return BSRRecord(asin=asin, month=month, monthly_sales=sales, seller_region=region)


# ---------------------------------------------------------------- T2-1 cross-tab

CROSSTAB_ROWS = [
    rec("A", "2026-01", 60, "CN"),
    rec("B", "2026-01", 40, "US"),
    rec("C", "2026-01", 30, "CN"),
    rec("D", "2026-01", 20, ""),  # UNKNOWN region
    rec("E", "2026-01", 10, "CN"),
]
CROSSTAB_CLS = {"A": "s1", "B": "s1", "C": "s2", "D": "s2", "E": "s1"}


def _cells():
    result = build_market_metrics(CROSSTAB_ROWS, CROSSTAB_CLS)
    return {(r.month, r.seller_region, r.segment): r for r in result.region_segments}


def test_crosstab_sales_and_asin_counts_cell_by_cell():
    cells = _cells()
    expected = {  # (region, segment): (sales, asin_count)
        ("CN", "s1"): (70, 2),
        ("CN", "s2"): (30, 1),
        ("US", "s1"): (40, 1),
        ("US", "s2"): (0, 0),
        ("UNKNOWN", "s1"): (0, 0),
        ("UNKNOWN", "s2"): (20, 1),
    }
    assert len(cells) == len(expected)  # dense: zero cells are kept
    for (region, segment), (sales, count) in expected.items():
        cell = cells[("2026-01", region, segment)]
        assert (cell.sales, cell.asin_count) == (sales, count), (region, segment)


def test_crosstab_denominators_with_and_without_unknown_are_explicit():
    cells = _cells()
    # month total includes UNKNOWN: 160 sales, 5 ASINs
    assert math.isclose(cells[("2026-01", "CN", "s1")].sales_share_of_month_pct, 43.75)
    assert math.isclose(cells[("2026-01", "UNKNOWN", "s2")].sales_share_of_month_pct, 12.5)
    assert math.isclose(cells[("2026-01", "CN", "s1")].asin_share_of_month_pct, 40.0)
    # known-region denominator excludes UNKNOWN: 140 sales
    assert math.isclose(cells[("2026-01", "CN", "s1")].sales_share_of_known_region_pct, 50.0)
    assert math.isclose(cells[("2026-01", "US", "s1")].sales_share_of_known_region_pct, 40 / 140 * 100)
    assert cells[("2026-01", "UNKNOWN", "s2")].sales_share_of_known_region_pct is None
    # within-region column share
    assert math.isclose(cells[("2026-01", "CN", "s1")].share_within_region_pct, 70.0)
    assert math.isclose(cells[("2026-01", "US", "s1")].share_within_region_pct, 100.0)
    assert cells[("2026-01", "UNKNOWN", "s1")].share_within_region_pct == 0.0


def test_crosstab_cells_sum_to_region_and_market_totals():
    result = build_market_metrics(CROSSTAB_ROWS, CROSSTAB_CLS)
    assert sum(c.sales for c in result.region_segments) == 160
    cn = sum(c.sales for c in result.region_segments if c.seller_region == "CN")
    region_row = next(r for r in result.seller_regions if r.seller_region == "CN")
    assert cn == region_row.sales


def test_crosstab_is_serialisable_and_old_keys_preserved():
    d = build_market_metrics(CROSSTAB_ROWS, CROSSTAB_CLS).to_dict()
    json.dumps(d)
    for key in ("market", "segments", "seller_regions", "decompositions", "period_comparisons"):
        assert key in d
    assert d["region_segments"]


# ---------------------------------------------------------------- T2-2 region trend

def _two_region(month, cn, us):
    return [rec(f"CN-{month}", month, cn, "CN"), rec(f"US-{month}", month, us, "US")]


COMPLETE = (
    _two_region("2025-01", 60, 40)
    + _two_region("2025-02", 50, 50)
    + _two_region("2026-01", 90, 30)
    + _two_region("2026-02", 40, 60)
)
# same data but 2025-02 is absent (coverage gap, not a real decline)
GAPPED = (
    _two_region("2025-01", 60, 40)
    + _two_region("2026-01", 90, 30)
    + _two_region("2026-02", 40, 60)
)


def _region(rows, month, region):
    result = build_market_metrics(rows, {})
    return next(r for r in result.seller_regions if r.month == month and r.seller_region == region)


def test_region_mom_yoy_on_complete_history():
    cn = _region(COMPLETE, "2026-02", "CN")
    assert math.isclose(cn.sales_mom_pct, (40 / 90 - 1) * 100)
    assert math.isclose(cn.sales_yoy_pct, -20.0)
    assert math.isclose(cn.share_mom_pp, 40.0 - 75.0)
    assert math.isclose(cn.share_yoy_pp, 40.0 - 50.0)
    assert cn.coverage_warning is None
    assert cn.cause_hypothesis is None


def test_region_yoy_withheld_with_coverage_warning_when_base_month_missing():
    cn = _region(GAPPED, "2026-02", "CN")
    assert cn.sales_yoy_pct is None and cn.share_yoy_pp is None
    assert cn.coverage_warning == "yoy_base_month_missing:2025-02"
    # the cause is only a to-be-verified hypothesis label, never a stated fact
    assert cn.cause_hypothesis.startswith("待验证假设")
    # MoM base (2026-01) exists, so MoM is still reported
    assert cn.sales_mom_pct is not None


def test_complete_and_gapped_history_are_distinguishable():
    full = _region(COMPLETE, "2026-02", "CN")
    gap = _region(GAPPED, "2026-02", "CN")
    assert (full.sales_yoy_pct is not None) and (gap.sales_yoy_pct is None)
    assert full.coverage_warning is None and gap.coverage_warning is not None


def test_region_zero_base_gives_no_pct_but_share_change_still_defined():
    rows = _two_region("2025-01", 100, 0) + _two_region("2026-01", 50, 50)
    us = _region(rows, "2026-01", "US")
    assert us.sales_yoy_pct is None  # zero base: no percentage
    assert math.isclose(us.share_yoy_pp, 50.0)
    assert us.coverage_warning is None or "yoy_base_month_missing" not in us.coverage_warning


def test_mom_gap_inside_range_warns_but_series_start_does_not():
    rows = _two_region("2026-01", 10, 10) + _two_region("2026-03", 10, 10)
    first = _region(rows, "2026-01", "CN").coverage_warning
    assert first is None or "mom_base_month_missing" not in first
    assert "mom_base_month_missing:2026-02" in _region(rows, "2026-03", "CN").coverage_warning


# ---------------------------------------------------------------- T2-2 seasonality

def _year(year, values):
    return {f"{year}-{m:02d}": v for m, v in enumerate(values, start=1)}


Y_A = [10, 5, 20, 20, 30, 40, 40, 50, 60, 70, 90, 80]  # high 11, low 02
Y_B = [12, 14, 6, 20, 30, 40, 40, 50, 60, 70, 95, 80]  # high 11, low 03


def test_seasonality_high_low_per_complete_year_and_recurring_month():
    series = {**_year(2024, Y_A), **_year(2025, Y_B)}
    res = detect_seasonality(series)
    by_year = {y.year: y for y in res.years}
    assert (by_year[2024].high_month, by_year[2024].low_month) == ("2024-11", "2024-02")
    assert (by_year[2024].high_sales, by_year[2024].low_sales) == (90, 5)
    assert (by_year[2025].high_month, by_year[2025].low_month) == ("2025-11", "2025-03")
    assert res.complete_years == [2024, 2025]
    assert res.recurring_high_month == "11"
    assert res.recurring_low_month is None  # low month moved between years
    assert all(y.coverage_warning is None for y in res.years)


def test_seasonality_ties_break_to_earlier_month():
    res = detect_seasonality(_year(2024, [5, 9, 9, 7, 7, 7, 7, 7, 7, 7, 5, 8]))
    y = res.years[0]
    assert y.high_month == "2024-02" and y.low_month == "2024-01"


def test_incomplete_year_is_coverage_issue_not_seasonality():
    series = {**_year(2024, Y_A), "2025-01": 1, "2025-02": 2, "2025-03": 3}
    res = detect_seasonality(series)
    y25 = next(y for y in res.years if y.year == 2025)
    assert not y25.is_complete
    assert y25.high_month is None and y25.low_month is None
    assert y25.months_observed == 3
    assert y25.missing_months[0] == "2025-04" and len(y25.missing_months) == 9
    assert y25.coverage_warning.startswith("incomplete_year")
    assert y25.cause_hypothesis.startswith("待验证假设")
    assert res.complete_years == [2024]
    assert res.recurring_high_month is None  # needs >= 2 complete years


def test_missing_month_in_otherwise_full_year_is_flagged_not_a_low_month():
    values = dict(_year(2024, Y_A))
    del values["2024-06"]
    y = detect_seasonality(values).years[0]
    assert not y.is_complete and y.missing_months == ["2024-06"]
    assert y.low_month is None  # a missing month must never be read as the seasonal low


def test_seasonality_wired_into_market_metrics_and_serialisable():
    rows = []
    for ym, v in {**_year(2024, Y_A), **_year(2025, Y_B)}.items():
        rows.append(rec("X", ym, v, "CN"))
    result = build_market_metrics(rows, {})
    assert result.seasonality.recurring_high_month == "11"
    d = result.to_dict()
    json.dumps(d)
    assert d["seasonality"]["complete_years"] == [2024, 2025]
