from __future__ import annotations

import math

from research_workflow.competition import build_competition_metrics
from research_workflow.data.models import BSRRecord


def rec(asin, month, sales, rank, brand):
    return BSRRecord(
        asin=asin,
        month=month,
        monthly_sales=sales,
        node_rank=rank,
        brand=brand,
    )


def test_asin_and_brand_concentration_use_sales_denominator():
    rows = [
        rec("A", "2026-01", 40, 1, "Brand1"),
        rec("B", "2026-01", 30, 2, "Brand1"),
        rec("C", "2026-01", 20, 3, "Brand2"),
        rec("D", "2026-01", 10, 4, "Brand3"),
    ]
    result = build_competition_metrics(rows, rank_cutoffs=(1, 3, 4))
    c = result.concentration[0]
    assert c.top3_asin_share_pct == 90.0
    assert c.top10_asin_share_pct == 100.0
    assert c.top3_brand_share_pct == 100.0
    assert c.top10_brand_share_pct == 100.0


def test_unknown_brands_do_not_collapse_into_one_fake_brand():
    rows = [
        rec("A", "2026-01", 30, 1, ""),
        rec("B", "2026-01", 25, 2, ""),
        rec("C", "2026-01", 20, 3, "Known"),
        rec("D", "2026-01", 15, 4, "Known2"),
        rec("E", "2026-01", 10, 5, "Known3"),
    ]
    c = build_competition_metrics(rows, rank_cutoffs=(5,)).concentration[0]
    # Top3 brands should be 30 + 25 + 20 = 75, not UNKNOWN grouped to 55.
    assert c.top3_brand_share_pct == 75.0


def test_rank_layers_use_bsr_order_not_sales_order():
    rows = [
        rec("A", "2026-01", 10, 1, "A"),
        rec("B", "2026-01", 90, 2, "B"),
        rec("C", "2026-01", 50, 3, "C"),
    ]
    result = build_competition_metrics(rows, rank_cutoffs=(1, 2, 3))
    layers = {(x.month, x.cutoff): x for x in result.rank_layers}
    assert layers[("2026-01", 1)].sales == 10
    assert layers[("2026-01", 2)].sales == 100
    assert layers[("2026-01", 3)].sales == 150


def test_head_up_broad_down_pattern_is_descriptive_signal_only():
    rows = []
    # 2025: top10 represented here by first two rows = 50, broad = 100
    rows += [rec("A", "2025-01", 30, 1, "A"), rec("B", "2025-01", 20, 2, "B")]
    rows += [rec("C", "2025-01", 50, 3, "C")]
    # 2026: head grows 20% to 60, broad declines 10% to 90
    rows += [rec("A", "2026-01", 35, 1, "A"), rec("B", "2026-01", 25, 2, "B")]
    rows += [rec("C", "2026-01", 30, 3, "C")]
    result = build_competition_metrics(rows, rank_cutoffs=(2, 3), head_cutoff=2, broad_cutoff=3)
    signal = result.head_suction[0]
    assert math.isclose(signal.head_yoy_pct, 20.0)
    assert math.isclose(signal.broad_yoy_pct, -10.0)
    assert signal.pattern == "head_up_broad_down"
    assert math.isclose(signal.head_minus_broad_pp, 30.0)


def test_non_cumulative_rank_bands_separate_head_waist_tail():
    rows = [rec(chr(65+i), "2026-01", 100-i*10, i+1, f"B{i}") for i in range(10)]
    result = build_competition_metrics(
        rows,
        rank_cutoffs=(5, 10),
        rank_bands=((1, 2, "head"), (3, 5, "waist"), (6, 10, "tail")),
        head_cutoff=2,
        broad_cutoff=10,
    )
    bands = {x.band: x for x in result.rank_bands}
    assert bands["head"].sales == 190
    assert bands["waist"].sales == 210
    assert bands["tail"].sales == 150
    assert sum(x.sales for x in bands.values()) == sum(r.monthly_sales for r in rows)
