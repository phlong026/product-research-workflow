from __future__ import annotations

import math

from research_workflow.data.models import BSRRecord
from research_workflow.metrics import build_market_metrics, compare_metrics


def rec(asin, month, sales, region="CN"):
    return BSRRecord(
        asin=asin,
        month=month,
        monthly_sales=sales,
        seller_region=region,
    )


def test_market_segment_region_and_yoy_metrics_are_deterministic():
    rows = [
        rec("A", "2025-01", 60, "CN"),
        rec("B", "2025-01", 40, "US"),
        rec("A", "2026-01", 50, "CN"),
        rec("C", "2026-01", 30, "US"),
    ]
    result = build_market_metrics(rows, {"A": "nugget", "B": "bullet", "C": "bullet"})

    market = {row.month: row for row in result.market}
    assert market["2025-01"].total_sales == 100
    assert market["2026-01"].total_sales == 80
    assert math.isclose(market["2026-01"].yoy_pct, -20.0)

    segments = {(row.month, row.segment): row for row in result.segments}
    assert segments[("2025-01", "nugget")].sales_share_pct == 60.0
    assert segments[("2026-01", "nugget")].sales_share_pct == 62.5
    assert math.isclose(segments[("2026-01", "nugget")].yoy_pct, -16.666666666666664)

    regions = {(row.month, row.seller_region): row for row in result.seller_regions}
    assert regions[("2026-01", "CN")].sales == 50
    assert regions[("2026-01", "US")].sales == 30


def test_segment_decomposition_reconstructs_exact_change():
    rows = [
        rec("N1", "2025-01", 20),
        rec("B1", "2025-01", 80),
        rec("N1", "2026-01", 25),
        rec("B1", "2026-01", 55),
    ]
    result = build_market_metrics(
        rows,
        {"N1": "nugget", "B1": "bullet"},
        decomposition_pairs=[("2025-01", "2026-01")],
    )
    nugget = next(row for row in result.decompositions if row.segment == "nugget")
    assert nugget.total_change_sales == 5.0
    assert math.isclose(nugget.market_change_pct, -20.0)
    assert nugget.segment_change_pct == 25.0
    assert math.isclose(nugget.share_change_pp, 11.25)
    assert math.isclose(nugget.reconstructed_change_sales, nugget.total_change_sales)
    # Market shrinking hurts this segment, share gain more than offsets it.
    assert nugget.market_contribution_sales < 0
    assert nugget.share_contribution_sales > 0


def test_mom_metrics_require_previous_calendar_month_not_previous_row():
    rows = [rec("A", "2026-01", 100), rec("A", "2026-03", 200)]
    result = build_market_metrics(rows, {"A": "nugget"})
    market = {row.month: row for row in result.market}
    assert market["2026-03"].mom_pct is None


def test_baseline_comparison_never_injects_expected_values():
    checks = compare_metrics(
        {"top3": 21.71, "market_yoy": -23.55},
        {"top3": 21.7, "market_yoy": -23.6, "missing": 1},
        {"top3": 0.02, "market_yoy": 0.1},
    )
    assert len(checks) == 2
    assert all(check.passed for check in checks)


def test_period_comparison_supports_jan_apr_business_window():
    rows = []
    for year, values in [(2025, [100, 100, 100, 100]), (2026, [80, 80, 80, 80])]:
        for month, value in enumerate(values, start=1):
            rows.append(rec("N", f"{year}-{month:02d}", value // 2))
            rows.append(rec("B", f"{year}-{month:02d}", value // 2))
    result = build_market_metrics(
        rows,
        {"N": "nugget", "B": "bullet"},
        period_comparison_pairs=[("2025-01", "2025-04", "2026-01", "2026-04")],
    )
    market = next(x for x in result.period_comparisons if x.scope == "market")
    assert market.base_sales == 400
    assert market.current_sales == 320
    assert math.isclose(market.change_pct, -20.0)
    nugget = next(x for x in result.period_comparisons if x.scope == "segment" and x.segment == "nugget")
    assert nugget.share_change_pp == 0.0
