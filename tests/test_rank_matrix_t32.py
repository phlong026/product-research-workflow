"""T3-2 point-rank (single rank) sales matrix, coexisting with cumulative TopN (synthetic data)."""
from __future__ import annotations

import json
import math

import pytest

from research_workflow.competition import (
    DEFAULT_POINT_RANKS,
    build_competition_metrics,
    build_point_rank_matrix,
    compare_point_rank_matrices,
)
from research_workflow.data.models import BSRRecord


def rec(asin, month, sales, sales_rank=None, node_rank=None):
    return BSRRecord(asin=asin, month=month, monthly_sales=sales, sales_rank=sales_rank, node_rank=node_rank)


def _row(matrix, month):
    return next(r for r in matrix.rows if r.month == month)


def test_default_ranks_match_business_spec():
    assert DEFAULT_POINT_RANKS == (1, 2, 3, 4, 5, 10, 20, 30, 50, 70, 100)


def test_point_sales_follow_sales_rank_position_and_pad_with_none_not_zero():
    # sales_rank order deliberately differs from sales order: sales_rank wins.
    rows = [
        rec("a", "2026-01", 30, sales_rank=3),
        rec("b", "2026-01", 50, sales_rank=1),
        rec("c", "2026-01", 60, sales_rank=2),
        rec("d", "2026-01", 20, sales_rank=4),
    ]
    m = build_point_rank_matrix(rows, ranks=(1, 2, 3, 4, 5, 10))
    row = _row(m, "2026-01")
    assert row.ordering_basis == "sales_rank"
    assert row.point_sales == {"1": 50, "2": 60, "3": 30, "4": 20, "5": None, "10": None}
    assert row.point_asin["1"] == "b" and row.point_asin["5"] is None


def test_without_sales_rank_orders_by_sales_desc_with_asin_tie_break():
    rows = [rec("z", "2026-01", 10), rec("a", "2026-01", 10), rec("m", "2026-01", 40)]
    row = _row(build_point_rank_matrix(rows, ranks=(1, 2, 3)), "2026-01")
    assert row.ordering_basis == "monthly_sales_desc"
    assert row.point_sales == {"1": 40, "2": 10, "3": 10}
    assert [row.point_asin[k] for k in ("1", "2", "3")] == ["m", "a", "z"]


def test_node_rank_is_not_used_for_point_ordering():
    rows = [rec("a", "2026-01", 5, node_rank=1), rec("b", "2026-01", 9, node_rank=2)]
    row = _row(build_point_rank_matrix(rows, ranks=(1, 2)), "2026-01")
    assert row.point_sales == {"1": 9, "2": 5}


def test_mixed_sales_rank_puts_ranked_first_then_remaining_by_sales():
    rows = [
        rec("a", "2026-01", 5, sales_rank=1),
        rec("b", "2026-01", 99),
        rec("c", "2026-01", 50),
    ]
    row = _row(build_point_rank_matrix(rows, ranks=(1, 2, 3)), "2026-01")
    assert row.ordering_basis == "mixed"
    assert row.point_sales == {"1": 5, "2": 99, "3": 50}


def test_zero_sales_cell_is_zero_not_none():
    rows = [rec("a", "2026-01", 7), rec("b", "2026-01", 0)]
    row = _row(build_point_rank_matrix(rows, ranks=(1, 2, 3)), "2026-01")
    assert row.point_sales == {"1": 7, "2": 0, "3": None}


def _month_rows(month, count, start=1000):
    # descending sales: position p (1-based) has sales start - p
    return [rec(f"{month}-{p:03d}", month, start - p, sales_rank=p) for p in range(1, count + 1)]


def test_default_matrix_against_hand_computed_values_with_fewer_than_100_asins():
    rows = _month_rows("2026-01", 120) + _month_rows("2026-02", 60, start=500)
    m = build_point_rank_matrix(rows)
    jan = _row(m, "2026-01")
    # position p -> 1000 - p
    assert jan.point_sales == {str(r): 1000 - r for r in DEFAULT_POINT_RANKS}
    feb = _row(m, "2026-02")
    expected = {str(r): 500 - r for r in (1, 2, 3, 4, 5, 10, 20, 30, 50)}
    expected.update({"70": None, "100": None})  # only 60 ASINs: no zero padding
    assert feb.point_sales == expected
    assert [r.month for r in m.rows] == ["2026-01", "2026-02"]
    assert m.ranks == list(DEFAULT_POINT_RANKS)


def test_ranks_are_configurable_deduped_sorted_and_validated():
    m = build_point_rank_matrix([rec("a", "2026-01", 1)], ranks=(3, 1, 3))
    assert m.ranks == [1, 3]
    with pytest.raises(ValueError):
        build_point_rank_matrix([], ranks=(0,))


def test_matrix_labels_basis_point_and_serialises():
    m = build_point_rank_matrix(_month_rows("2026-01", 5), ranks=(1, 5))
    d = m.to_dict()
    json.dumps(d)
    assert d["basis"] == "point"
    assert d["rows"][0]["point_sales"] == {"1": 999, "5": 995}


# ------------------------------------------------------------------ comparison

def _two_period_matrix():
    rows = (
        [rec("p1", "2025-01", 100, sales_rank=1), rec("p2", "2025-01", 80, sales_rank=2),
         rec("p3", "2025-01", 50, sales_rank=3), rec("p4", "2025-01", 0, sales_rank=4),
         rec("p5", "2025-01", 40, sales_rank=5)]
        + [rec("c1", "2026-01", 120, sales_rank=1), rec("c2", "2026-01", 60, sales_rank=2),
           rec("c4", "2026-01", 10, sales_rank=3), rec("c5", "2026-01", 40, sales_rank=4)]
    )
    return build_point_rank_matrix(rows, ranks=(1, 2, 3, 4, 5))


def test_compare_two_periods_absolute_and_relative_change_by_rank():
    changes = compare_point_rank_matrices(_two_period_matrix(), pairs=[("2025-01", "2026-01")])
    by_rank = {c.rank: c for c in changes}
    assert (by_rank[1].abs_change, math.isclose(by_rank[1].rel_change_pct, 20.0)) == (20, True)
    assert by_rank[1].direction == "up"
    assert (by_rank[2].abs_change, math.isclose(by_rank[2].rel_change_pct, -25.0)) == (-20, True)
    assert by_rank[2].direction == "down"
    assert by_rank[3].abs_change == -40 and by_rank[3].direction == "down"
    # base 0 -> absolute change defined, relative change not
    assert by_rank[4].base_point_sales == 0 and by_rank[4].current_point_sales == 40
    assert by_rank[4].abs_change == 40 and by_rank[4].rel_change_pct is None
    assert by_rank[4].direction == "up"
    # current period lacks a 5th ASIN: unavailable, never a fake -100%
    assert by_rank[5].current_point_sales is None
    assert by_rank[5].abs_change is None and by_rank[5].rel_change_pct is None
    assert by_rank[5].direction == "unavailable"
    assert all(c.basis == "point" for c in changes)


def test_compare_defaults_to_year_over_year_pairs_present_in_matrix():
    m = _two_period_matrix()
    changes = compare_point_rank_matrices(m)
    assert {(c.base_month, c.current_month) for c in changes} == {("2025-01", "2026-01")}
    # no prior-year month in data -> no pair and no invented comparison
    assert compare_point_rank_matrices(build_point_rank_matrix(_month_rows("2026-01", 3), ranks=(1,))) == []


def test_flat_direction_when_equal():
    rows = [rec("a", "2025-01", 10), rec("b", "2026-01", 10)]
    ch = compare_point_rank_matrices(build_point_rank_matrix(rows, ranks=(1,)))
    assert ch[0].direction == "flat" and ch[0].abs_change == 0


def test_changes_are_factual_signals_only():
    changes = compare_point_rank_matrices(_two_period_matrix(), pairs=[("2025-01", "2026-01")])
    assert {c.direction for c in changes} <= {"up", "down", "flat", "unavailable"}


# ------------------------------------------------------ coexistence with cumulative

def test_point_matrix_coexists_with_cumulative_layers_and_labels_basis():
    rows = [
        rec("a", "2026-01", 50, sales_rank=1, node_rank=1),
        rec("b", "2026-01", 30, sales_rank=2, node_rank=2),
        rec("c", "2026-01", 20, sales_rank=3, node_rank=3),
    ]
    result = build_competition_metrics(rows, rank_cutoffs=(1, 3), point_ranks=(1, 2, 3))
    layers = {x.cutoff: x for x in result.rank_layers}
    assert layers[3].sales == 100 and layers[3].basis == "cumulative"  # cumulative unchanged
    assert all(b.basis == "cumulative" for b in result.rank_bands)
    row = result.point_rank_matrix.rows[0]
    assert row.point_sales == {"1": 50, "2": 30, "3": 20}  # point, not cumulative
    d = result.to_dict()
    json.dumps(d)
    for key in ("concentration", "rank_layers", "rank_bands", "head_suction",
                "point_rank_matrix", "point_rank_changes"):
        assert key in d
    assert d["point_rank_matrix"]["basis"] == "point"
