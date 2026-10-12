from __future__ import annotations

import math

from research_workflow.new_products import (
    NewProductConfig,
    NewProductSnapshot,
    analyze_new_products,
)


def snap(asin, month, sales, account="SC", launch=None, eligible=True):
    return NewProductSnapshot(
        asin=asin,
        month=month,
        monthly_sales=sales,
        account_type=account,
        launch_date=launch,
        eligible=eligible,
    )


def test_launch_success_requires_consecutive_calendar_months():
    rows = [
        snap("A", "2026-01", 300),
        snap("A", "2026-02", 260),
        snap("A", "2026-04", 400),  # March missing: streak breaks
    ]
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    outcome = result.outcomes[0]
    assert outcome.launch_success is False
    assert outcome.success_month is None


def test_success_survival_and_early_threshold_are_separate_concepts():
    rows = [
        snap("A", "2026-01", 300, "VC", "2026-01-15"),
        snap("A", "2026-02", 350, "VC"),
        snap("A", "2026-03", 330, "VC"),
        snap("A", "2026-04", 100, "VC"),
        snap("B", "2026-01", 10, "SC"),
        snap("B", "2026-02", 300, "SC"),
        snap("B", "2026-03", 300, "SC"),
        snap("B", "2026-04", 300, "SC"),
    ]
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    outcomes = {x.asin: x for x in result.outcomes}

    assert outcomes["A"].launch_success is True
    assert outcomes["A"].success_month == "2026-03"
    assert outcomes["A"].first_threshold_month_index == 1
    assert outcomes["A"].current_survival is False

    assert outcomes["B"].launch_success is True
    assert outcomes["B"].success_month == "2026-04"
    assert outcomes["B"].first_threshold_month_index == 2
    assert outcomes["B"].current_survival is True

    assert result.summary.successful_products == 2
    assert result.summary.surviving_successful_products == 1
    assert result.summary.successful_with_early_threshold == 2
    assert result.summary.early_threshold_share_among_successful_pct == 100.0


def test_vc_metrics_use_successful_products_without_implying_business_decision():
    rows = []
    for asin, account, sales in [
        ("V1", "VC", [300, 300, 300]),
        ("V2", "Vendor Central", [10, 10, 10]),
        ("S1", "SC", [300, 300, 300]),
        ("S2", "3P", [10, 10, 10]),
    ]:
        for i, value in enumerate(sales, start=1):
            rows.append(snap(asin, f"2026-0{i}", value, account))
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    metrics = {x.account_type: x for x in result.account_types}
    assert metrics["VC"].total_products == 2
    assert metrics["VC"].successful_products == 1
    assert metrics["VC"].success_rate_pct == 50.0
    assert metrics["SC"].success_rate_pct == 50.0
    assert math.isclose(metrics["VC"].share_of_successful_pct, 50.0)


def test_duplicate_downloads_and_ineligible_old_variants_are_handled():
    rows = [
        snap("A", "2026-01", 200),
        snap("A", "2026-01", 300),  # duplicate download: keep higher
        snap("A", "2026-02", 300),
        snap("A", "2026-03", 300),
        snap("OLD", "2026-01", 999, eligible=False),
    ]
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    assert result.summary.total_products == 1
    assert result.outcomes[0].asin == "A"
    assert result.outcomes[0].launch_success is True


def test_missing_global_current_month_is_unknown_not_zero_sales():
    rows = [
        snap("A", "2026-01", 300, "VC"),
        snap("A", "2026-02", 300, "VC"),
        snap("A", "2026-03", 300, "VC"),
        snap("B", "2026-04", 10, "SC"),
    ]
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    outcome = next(x for x in result.outcomes if x.asin == "A")
    assert outcome.launch_success is True
    assert outcome.current_month == "2026-04"
    assert outcome.current_observed is False
    assert outcome.current_sales is None
    assert outcome.current_survival is None


def test_new_product_group_metrics_cover_region_and_segment():
    rows = [
        NewProductSnapshot("A", "2026-01", 300, seller_region="CN", account_type="VC", segment="nugget"),
        NewProductSnapshot("A", "2026-02", 300, seller_region="CN", account_type="VC", segment="nugget"),
        NewProductSnapshot("A", "2026-03", 300, seller_region="CN", account_type="VC", segment="nugget"),
        NewProductSnapshot("B", "2026-01", 10, seller_region="US", account_type="SC", segment="bullet"),
        NewProductSnapshot("B", "2026-02", 10, seller_region="US", account_type="SC", segment="bullet"),
        NewProductSnapshot("B", "2026-03", 10, seller_region="US", account_type="SC", segment="bullet"),
    ]
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3))
    regions = {x.value: x for x in result.seller_regions}
    segments = {x.value: x for x in result.segments}
    assert regions["CN"].success_rate_pct == 100.0
    assert regions["US"].success_rate_pct == 0.0
    assert segments["nugget"].successful_products == 1
