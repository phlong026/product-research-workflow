"""T3-1 seller_id concentration basis next to ASIN and brand bases (synthetic data)."""
from __future__ import annotations

import json
import math

from research_workflow.competition import build_competition_metrics
from research_workflow.data.models import BSRRecord


def rec(asin, sales, brand, seller, month="2026-01"):
    return BSRRecord(asin=asin, month=month, monthly_sales=sales, brand=brand, seller_id=seller)


def _conc(rows):
    return build_competition_metrics(rows, rank_cutoffs=(1,)).concentration[0]


def test_three_bases_give_three_different_top3_values():
    # S1 owns three ASINs of three brands; brand X is spread over three sellers.
    rows = [
        rec("a1", 30, "X", "S1"),
        rec("a2", 25, "Y", "S1"),
        rec("a3", 20, "Z", "S1"),
        rec("a4", 10, "X", "S2"),
        rec("a5", 8, "X", "S3"),
        rec("a6", 7, "Y", "S4"),
    ]
    c = _conc(rows)
    assert math.isclose(c.top3_asin_share_pct, 75.0)    # 30+25+20
    assert math.isclose(c.top3_brand_share_pct, 100.0)  # X=48, Y=32, Z=20
    assert math.isclose(c.top3_seller_share_pct, 93.0)  # S1=75, S2=10, S3=8
    assert len({c.top3_asin_share_pct, c.top3_brand_share_pct, c.top3_seller_share_pct}) == 3
    assert c.unknown_seller_sales == 0 and c.unknown_seller_asin_count == 0


def test_three_bases_give_three_different_top10_values():
    # 12 ASINs x 10 sales; a1..a3 share brand B and a1,a2 share seller S.
    rows = [rec("a1", 10, "B", "S"), rec("a2", 10, "B", "S"), rec("a3", 10, "B", "T3")]
    rows += [rec(f"a{i}", 10, f"b{i}", f"T{i}") for i in range(4, 13)]
    c = _conc(rows)
    assert math.isclose(c.top10_asin_share_pct, 100 / 120 * 100)
    assert math.isclose(c.top10_seller_share_pct, 110 / 120 * 100)  # S=20 + 9 sellers x 10
    assert math.isclose(c.top10_brand_share_pct, 100.0)  # only 10 brands exist
    assert len({round(c.top10_asin_share_pct, 6), round(c.top10_seller_share_pct, 6),
                round(c.top10_brand_share_pct, 6)}) == 3


def test_missing_seller_id_is_unknown_and_never_merged_into_one_seller():
    rows = [
        rec("u1", 30, "A", ""),
        rec("u2", 20, "B", "   "),
        rec("u3", 10, "C", ""),
        rec("k1", 3, "D", "K"),
        rec("k2", 2, "D", "K"),
        rec("l1", 4, "E", "L"),
    ]
    c = _conc(rows)
    # merged-unknown would give (60+5+4)/69 = 100%; separate buckets give 30+20+10.
    assert math.isclose(c.top3_seller_share_pct, 60 / 69 * 100)
    assert c.unknown_seller_sales == 60
    assert c.unknown_seller_asin_count == 3
    assert math.isclose(c.unknown_seller_share_pct, 60 / 69 * 100)


def test_all_known_sellers_report_zero_unknown_and_old_fields_unchanged():
    rows = [rec("a", 40, "B1", "S"), rec("b", 30, "B1", "T"), rec("c", 30, "B2", "U")]
    c = _conc(rows)
    assert c.unknown_seller_share_pct == 0.0
    assert math.isclose(c.top3_seller_share_pct, 100.0)
    assert c.total_sales == 100 and c.asin_count == 3
    d = build_competition_metrics(rows, rank_cutoffs=(1,)).to_dict()
    json.dumps(d)
    for key in ("top3_asin_share_pct", "top10_asin_share_pct", "top3_brand_share_pct",
                "top10_brand_share_pct", "top3_seller_share_pct", "top10_seller_share_pct",
                "unknown_seller_sales", "unknown_seller_asin_count", "unknown_seller_share_pct"):
        assert key in d["concentration"][0]
