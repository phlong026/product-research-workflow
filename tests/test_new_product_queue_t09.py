from __future__ import annotations

import pytest

from research_workflow.new_products import (
    NewProductQueue,
    NewProductSnapshot,
    build_new_product_queue,
)


def row(asin, month, launch=None, parent="", eligible=True):
    return NewProductSnapshot(
        asin=asin,
        month=month,
        monthly_sales=100,
        launch_date=launch,
        parent_asin=parent,
        eligible=eligible,
    )


def items_by_asin(queue: NewProductQueue):
    return {item.asin: item for item in queue.items}


def exclusions(queue: NewProductQueue):
    return sorted((e.asin, e.reason) for e in queue.exclusions)


def test_window_is_measured_to_each_crawl_month_end():
    rows = [
        row("A", "2026-01", "2026-01-10"),
        row("A", "2026-02", "2026-01-10"),
        row("A", "2026-03", "2026-01-10"),
        row("A", "2026-04", "2026-01-10"),
        row("OLD", "2026-03", "2025-06-01"),  # far outside the window: simply not new
    ]
    queue = build_new_product_queue(rows, study_month="2026-04", window_days=90)
    assert set(items_by_asin(queue)) == {"A"}
    a = queue.items[0]
    # Jan 31 (21d), Feb 28 (49d), Mar 31 (80d) are within 90 days; Apr 30 is 110 days
    assert a.new_months == ("2026-01", "2026-02", "2026-03")
    assert a.first_new_month == "2026-01"
    assert a.launch_month == "2026-01"
    assert queue.exclusions == []


def test_cross_quarter_boundary_coverage_gap_is_reported():
    # crawl month ends: Jan 31, Apr 30, Jul 31. May 1 -> Jul 31 is 91 days (> 90): May is a gap.
    rows = [
        row("A", "2026-01", "2026-01-05"),
        row("B", "2026-04", "2026-03-10"),
        row("C", "2026-07", "2026-06-20"),
    ]
    queue = build_new_product_queue(rows, study_month="2026-07", window_days=90)
    assert queue.coverage_gaps == ["2026-05"]
    cov = {c.month: c for c in queue.coverage}
    assert list(cov) == [f"2026-0{m}" for m in range(1, 8)]
    assert cov["2026-05"].covered is False and cov["2026-05"].covering_crawl_month is None
    # Feb and Mar are covered by the April crawl, crossing the Q1 -> Q2 boundary
    assert cov["2026-02"].covering_crawl_month == "2026-04"
    assert cov["2026-02"].crosses_quarter_boundary is True
    assert cov["2026-03"].crosses_quarter_boundary is True
    assert cov["2026-01"].covering_crawl_month == "2026-01"
    assert cov["2026-01"].crosses_quarter_boundary is False
    assert cov["2026-06"].crosses_quarter_boundary is True  # Q2 month covered by the July (Q3) crawl
    assert cov["2026-03"].quarter == "2026-Q1"
    # per-month new product counts follow the launch month
    assert cov["2026-03"].new_product_count == 1
    assert cov["2026-05"].new_product_count == 0


def test_quarterly_crawl_has_no_gaps():
    rows = [row("A", "2026-03", "2026-01-15"), row("B", "2026-06", "2026-04-02")]
    queue = build_new_product_queue(rows, study_month="2026-06", window_days=90)
    assert queue.coverage_gaps == []
    assert {i.asin for i in queue.items} == {"A", "B"}


def test_old_parent_variant_is_marked_and_excluded_by_default():
    rows = [
        row("PARENTSEED", "2026-01", "2025-05-01", parent="PAR1"),  # existing family member
        row("PAR3", "2026-01", "2025-05-01"),  # a parent that is itself an ASIN
        row("NEW1", "2026-02", "2026-01-20", parent="PAR1"),  # sibling already existed -> old parent
        row("NEW2", "2026-02", "2026-01-25", parent="PAR2"),  # brand new family
        row("NEW3", "2026-02", "2026-01-26", parent="PAR3"),  # parent ASIN existed earlier
        row("NEW4", "2026-02", "2026-01-27"),  # no parent info: cannot be flagged
    ]
    queue = build_new_product_queue(rows, study_month="2026-02", window_days=90)
    assert set(items_by_asin(queue)) == {"NEW2", "NEW4"}
    assert exclusions(queue) == [("NEW1", "old_parent"), ("NEW3", "old_parent")]
    assert queue.excluded_asins("old_parent") == ["NEW1", "NEW3"]


def test_manual_flags_override_automatic_decision():
    rows = [
        row("PARENTSEED", "2026-01", "2025-05-01", parent="PAR1"),
        row("NEW1", "2026-02", "2026-01-20", parent="PAR1"),  # auto: old parent
        row("NEW2", "2026-02", "2026-01-25", parent="PAR2"),  # auto: eligible
        row("NEW3", "2026-02", "2026-01-26", eligible=False),  # snapshot flag False = manual exclusion
    ]
    queue = build_new_product_queue(
        rows,
        study_month="2026-02",
        manual_eligible={"NEW1": True, "NEW2": False},
    )
    items = items_by_asin(queue)
    assert set(items) == {"NEW1"}
    assert items["NEW1"].old_parent_variant is True  # still marked
    assert items["NEW1"].manually_included is True  # but manual True wins
    assert exclusions(queue) == [("NEW2", "manual_ineligible"), ("NEW3", "manual_ineligible")]


def test_manual_false_beats_old_parent_reason():
    rows = [
        row("PARENTSEED", "2026-01", "2025-05-01", parent="PAR1"),
        row("NEW1", "2026-02", "2026-01-20", parent="PAR1"),
    ]
    queue = build_new_product_queue(rows, study_month="2026-02", manual_eligible={"new1": False})
    assert exclusions(queue) == [("NEW1", "manual_ineligible")]


def test_duplicate_asin_month_rows_are_listed_and_asin_kept_once():
    rows = [
        row("a", "2026-02", "2026-01-20"),
        row("A", "2026-02", "2026-01-20"),  # duplicate download (case-insensitive ASIN)
        row("A", "2026-02", "2026-01-20"),
    ]
    queue = build_new_product_queue(rows, study_month="2026-02")
    assert [i.asin for i in queue.items] == ["A"]
    dup = [e for e in queue.exclusions if e.reason == "duplicate"]
    assert len(dup) == 2 and all(e.asin == "A" and e.month == "2026-02" for e in dup)


def test_rows_without_valid_launch_date_are_listed_not_silently_dropped():
    rows = [row("A", "2026-02", None), row("B", "2026-02", "not-a-date")]
    queue = build_new_product_queue(rows, study_month="2026-02")
    assert queue.items == []
    assert exclusions(queue) == [("A", "no_launch_date"), ("B", "no_launch_date")]


def test_study_month_must_match_latest_input_month_and_to_dict():
    rows = [row("A", "2026-02", "2026-01-20")]
    with pytest.raises(ValueError, match="study_month"):
        build_new_product_queue(rows, study_month="2026-03")
    with pytest.raises(ValueError, match="window_days"):
        build_new_product_queue(rows, study_month="2026-02", window_days=0)
    d = build_new_product_queue(rows, study_month="2026-02").to_dict()
    assert d["study_month"] == "2026-02" and d["window_days"] == 90
    assert d["items"][0]["asin"] == "A"
    assert {"items", "exclusions", "coverage", "coverage_gaps"} <= set(d)


def test_queue_is_deterministic_regardless_of_input_order():
    rows = [
        row("B", "2026-02", "2026-01-21"),
        row("A", "2026-02", "2026-01-20"),
        row("A", "2026-01", "2026-01-20"),
    ]
    assert build_new_product_queue(rows, study_month="2026-02").to_dict() == build_new_product_queue(
        list(reversed(rows)), study_month="2026-02"
    ).to_dict()
