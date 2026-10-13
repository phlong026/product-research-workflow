from datetime import date

import pytest

from research_workflow.data import MonthCoverage, available_month_window, month_coverage_report


def test_default_window_is_36_months_ending_at_previous_month():
    window = available_month_window(date(2026, 10, 13))
    assert len(window) == 36
    assert window[0] == "2023-10"
    assert window[-1] == "2026-09"
    assert window == sorted(window)
    assert len(set(window)) == 36


def test_window_crosses_year_boundary_when_lag_applied():
    window = available_month_window(date(2026, 1, 5), years=1)
    assert window[-1] == "2025-12"
    assert window[0] == "2025-01"
    assert len(window) == 12


def test_lag_zero_includes_current_month():
    window = available_month_window(date(2026, 3, 31), years=1, latest_available_lag_months=0)
    assert window[-1] == "2026-03"
    assert window[0] == "2025-04"


def test_larger_lag_crosses_multiple_years():
    window = available_month_window(date(2026, 2, 1), years=1, latest_available_lag_months=14)
    assert window[-1] == "2024-12"
    assert window[0] == "2024-01"


@pytest.mark.parametrize("years,lag", [(0, 1), (-1, 1), (3, -1)])
def test_invalid_window_arguments_rejected(years, lag):
    with pytest.raises(ValueError):
        available_month_window(date(2026, 1, 1), years=years, latest_available_lag_months=lag)


def test_coverage_complete():
    expected = ["2026-01", "2026-02", "2026-03"]
    report = month_coverage_report(expected, ["2026-03", "2026-01", "2026-02"])
    assert isinstance(report, MonthCoverage)
    assert report.complete is True
    assert report.missing == []
    assert report.extra == []


def test_coverage_lists_missing_months_explicitly_in_order():
    expected = ["2025-12", "2026-01", "2026-02", "2026-03"]
    report = month_coverage_report(expected, ["2026-01", "2026-03"])
    assert report.complete is False
    assert report.missing == ["2025-12", "2026-02"]
    assert report.extra == []


def test_coverage_reports_extra_months_without_breaking_completeness():
    report = month_coverage_report(["2026-01"], iter(["2026-01", "2025-06", "2025-06"]))
    assert report.complete is True
    assert report.extra == ["2025-06"]


def test_coverage_rejects_invalid_month_and_serializes():
    with pytest.raises(ValueError):
        month_coverage_report(["2026-13"], [])
    report = month_coverage_report(["2026-01", "2026-02"], ["2026-01"])
    assert report.to_dict() == {
        "expected": ["2026-01", "2026-02"],
        "present": ["2026-01"],
        "missing": ["2026-02"],
        "extra": [],
        "complete": False,
    }
