from __future__ import annotations

import math

import pytest

from research_workflow.new_products import (
    NewProductConfig,
    NewProductSnapshot,
    analyze_new_products,
)


def snap(asin, month, sales, launch=None):
    return NewProductSnapshot(asin=asin, month=month, monthly_sales=sales, launch_date=launch)


def series(asin, start_month, values, launch=None):
    """Consecutive natural months of 2026 starting at month number start_month."""
    return [
        snap(asin, f"2026-{start_month + i:02d}", v, launch if i == 0 else None)
        for i, v in enumerate(values)
    ]


def build_rows():
    rows = []
    rows += series("P1", 4, [300, 300, 300])  # success + retained
    rows += series("P2", 3, [300, 300, 300, 100])  # historically successful, current fails
    rows += series("P3", 3, [300, 300, 300])  # success, 2026-06 missing -> unknown
    rows += series("P4", 5, [400, 400])  # only 2 months -> insufficient
    rows += series("P5", 4, [10, 10, 10])  # never reaches threshold
    return rows


def test_retention_rate_hand_calculated_with_unknown_disclosed():
    result = analyze_new_products(build_rows(), NewProductConfig(250, 3, 3, study_month="2026-06"))
    s = result.summary
    status = {o.asin: o.retention_status for o in result.outcomes}
    assert status == {
        "P1": "retained",
        "P2": "not_retained",  # historical success but current month failed
        "P3": "unknown",  # current month missing: not treated as 0
        "P4": "not_retained",  # fewer than 3 months
        "P5": "not_retained",
    }
    # denominator = all 5 valid new products
    assert s.retention_denominator_products == 5
    assert s.retained_products == 1
    assert s.retention_unknown_products == 1
    assert math.isclose(s.retention_rate_pct, 20.0)  # 1/5 lower bound
    assert math.isclose(s.retention_rate_upper_bound_pct, 40.0)  # (1+1)/5
    assert "全部有效新品" in s.retention_denominator_note
    assert "未知" in s.retention_denominator_note
    # launch success (3/5) and legacy survival among observed successful (1/2) stay separate
    assert s.successful_products == 3
    assert math.isclose(s.success_rate_pct, 60.0)
    assert s.surviving_successful_products == 1
    assert s.unknown_successful_products == 1
    assert math.isclose(s.survival_rate_among_successful_pct, 50.0)  # observed-only denominator (P1, P2)


def test_two_thresholds_give_different_promotion_and_retention():
    rows = series("X", 4, [300, 300, 300]) + series("Y", 4, [400, 400, 400]) + series("Z", 4, [150, 150, 150])
    high_retention = analyze_new_products(
        rows,
        NewProductConfig(
            promotion_waist_sales_threshold=200,
            retention_sales_threshold=350,
            study_month="2026-06",
        ),
    )
    s = high_retention.summary
    assert (s.promotion_waist_sales_threshold, s.retention_sales_threshold) == (200, 350)
    assert s.successful_products == 2  # X, Y >= 200 for 3 months
    assert s.retained_products == 1  # only Y >= 350
    assert math.isclose(s.success_rate_pct, 200 / 3)
    assert math.isclose(s.retention_rate_pct, 100 / 3)

    high_promotion = analyze_new_products(
        rows,
        NewProductConfig(
            promotion_waist_sales_threshold=350,
            retention_sales_threshold=200,
            study_month="2026-06",
        ),
    )
    s = high_promotion.summary
    assert s.successful_products == 1  # Y only
    assert s.retained_products == 2  # X, Y
    d = high_promotion.to_dict()["summary"]
    assert d["promotion_waist_sales_threshold"] == 350
    assert d["retention_sales_threshold"] == 200


def test_defaults_keep_legacy_behaviour_with_single_threshold():
    cfg = NewProductConfig(sales_threshold=300)
    assert cfg.effective_promotion_threshold == 300
    assert cfg.effective_retention_threshold == 300
    cfg = NewProductConfig(sales_threshold=300, current_survival_threshold=100)
    assert cfg.effective_promotion_threshold == 300
    assert cfg.effective_retention_threshold == 100
    assert NewProductConfig().effective_promotion_threshold == 250
    assert NewProductConfig().effective_retention_threshold == 250


def test_early_window_denominator_is_complete_window_products_not_only_successful():
    rows = []
    rows += series("E1", 4, [300, 300, 300], launch="2026-04-10")  # window 04-06 complete, reached
    rows += series("E2", 4, [300, 300, 300], launch="2026-03-20")  # March unobserved -> incomplete
    rows += series("E3", 4, [10, 10, 10], launch="2026-04-02")  # complete, never reached
    rows += series("E4", 5, [300, 300], launch="2026-05-01")  # window 05-07 beyond study month
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3, study_month="2026-06"))
    s = result.summary
    assert s.total_products == 4
    assert s.early_window_complete_products == 2  # E1, E3
    assert s.early_window_incomplete_products == 2  # E2, E4
    assert s.early_window_reached_products == 1  # E1
    assert math.isclose(s.early_window_reached_share_pct, 50.0)
    # legacy field keeps its old (successful-only) denominator for compatibility
    assert s.successful_products == 2  # E1 and E2 have 3 consecutive months
    assert s.successful_with_early_threshold == 2


def test_launch_month_is_month_one_regardless_of_day():
    # launched on the 31st: that natural month is still month 1
    rows = series("L", 1, [300, 300, 300, 10, 300, 300], launch="2026-01-31")
    result = analyze_new_products(rows, NewProductConfig(250, 3, 3, study_month="2026-06"))
    o = result.outcomes[0]
    assert o.anchor_month == "2026-01"
    assert o.first_threshold_month_index == 1
    assert o.early_window_complete is True and o.early_window_reached is True


def test_study_month_must_equal_latest_input_month():
    rows = series("A", 4, [300, 300, 300])
    with pytest.raises(ValueError, match="study_month"):
        analyze_new_products(rows, NewProductConfig(study_month="2026-07"))
    with pytest.raises(ValueError, match="study_month"):
        analyze_new_products(rows, NewProductConfig(study_month="2026-05"))
    ok = analyze_new_products(rows, NewProductConfig(study_month="2026-06"))
    assert ok.summary.current_month == "2026-06"
    # without study_month nothing is enforced (legacy)
    assert analyze_new_products(rows).summary.current_month == "2026-06"


def test_new_fields_are_to_dict_serialisable_and_snapshot_parent_optional():
    result = analyze_new_products(build_rows(), NewProductConfig(study_month="2026-06"))
    d = result.to_dict()
    assert d["config"]["study_month"] == "2026-06"
    assert d["outcomes"][0]["retention_status"] in {"retained", "not_retained", "unknown"}
    assert NewProductSnapshot("A", "2026-01", 1).parent_asin == ""
