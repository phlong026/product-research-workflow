"""T2-4 product iteration progress: earliest valid launch date per segment (synthetic data)."""
from __future__ import annotations

import json
from datetime import date

from research_workflow.data.models import BSRRecord
from research_workflow.metrics import IterationConfig, build_iteration_comparison

AS_OF = date(2026, 6, 1)


def rec(asin, month, launch):
    return BSRRecord(asin=asin, month=month, monthly_sales=10, launch_date=launch)


ROWS = [
    # s1: valid 2018-05-10 / 2020-01-01 plus every kind of invalid date
    rec("A", "2026-01", "2018-05-10"),
    rec("B", "2026-02", "2020-01-01"),
    rec("C", "2026-01", None),
    rec("D", "2026-01", "2026-07-01"),  # future
    rec("E", "2026-01", "1985-01-01"),  # earlier than 1990
    rec("F", "2026-01", "not-a-date"),
    # s2: ONLY invalid dates, but observed in data since 2025-11
    rec("G", "2025-11", ""),
    rec("H", "2026-03", "2030-01-01"),
    # s3: valid, later than s1
    rec("I", "2026-04", "2019/03/01"),
]
CLS = {"A": "s1", "B": "s1", "C": "s1", "D": "s1", "E": "s1", "F": "s1",
       "G": "s2", "H": "s2", "I": "s3"}


def _by_segment(config=None):
    result = build_iteration_comparison(ROWS, CLS, config=config or IterationConfig(as_of=AS_OF))
    return result, {row.segment: row for row in result.segments}


def test_segments_are_ordered_by_earliest_valid_launch_date():
    result, by_seg = _by_segment()
    assert [r.segment for r in result.segments] == ["s1", "s3", "s2"]
    assert [r.order for r in result.segments] == [1, 2, None]  # no valid date: unranked, last
    assert by_seg["s1"].earliest_launch_date == "2018-05-10"
    assert by_seg["s1"].earliest_launch_asin == "A"
    assert by_seg["s3"].earliest_launch_date == "2019-03-01"


def test_abnormal_dates_are_excluded_and_counted_per_reason():
    _, by_seg = _by_segment()
    s1 = by_seg["s1"]
    assert s1.asin_count == 6
    assert s1.valid_launch_date_asin_count == 2
    assert s1.excluded_missing == 1       # C
    assert s1.excluded_future == 1        # D
    assert s1.excluded_too_early == 1     # E
    assert s1.excluded_unparseable == 1   # F


def test_earliest_observed_in_data_is_never_used_as_launch_date():
    _, by_seg = _by_segment()
    s1, s2 = by_seg["s1"], by_seg["s2"]
    assert s1.earliest_observed_in_data == "2026-01"   # data window start
    assert s1.earliest_launch_date == "2018-05-10"      # real launch, different field
    # all dates invalid: launch date stays None instead of falling back to the data month
    assert s2.earliest_launch_date is None
    assert s2.earliest_launch_asin is None
    assert s2.earliest_observed_in_data == "2025-11"


def test_as_of_boundary_is_valid_and_rules_are_configurable():
    rows = [rec("A", "2026-01", "2026-06-01"), rec("B", "2026-01", "1995-01-01")]
    cls = {"A": "s", "B": "s"}
    default = build_iteration_comparison(rows, cls, config=IterationConfig(as_of=AS_OF)).segments[0]
    assert default.valid_launch_date_asin_count == 2 and default.earliest_launch_date == "1995-01-01"
    strict = build_iteration_comparison(
        rows, cls, config=IterationConfig(as_of=AS_OF, min_launch_date=date(2000, 1, 1))
    ).segments[0]
    assert strict.valid_launch_date_asin_count == 1
    assert strict.excluded_too_early == 1
    assert strict.earliest_launch_date == "2026-06-01"


def test_asin_across_months_counted_once_and_unclassified_goes_to_unknown():
    rows = [rec("A", "2026-01", "2020-01-01"), rec("A", "2026-02", "2020-01-01"), rec("Z", "2026-02", "2021-01-01")]
    result = build_iteration_comparison(rows, {"A": "s"}, config=IterationConfig(as_of=AS_OF))
    by_seg = {r.segment: r for r in result.segments}
    assert by_seg["s"].asin_count == 1 and by_seg["s"].earliest_observed_in_data == "2026-01"
    assert by_seg["unknown"].asin_count == 1


def test_result_is_serialisable_and_documents_field_semantics():
    result, _ = _by_segment()
    d = result.to_dict()
    json.dumps(d)
    assert d["segments"][0]["segment"] == "s1"
    notes = " ".join(d["notes"])
    assert "earliest_observed_in_data" in notes and "earliest_launch_date" in notes
