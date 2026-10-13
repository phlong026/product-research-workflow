"""第一批口径整改：跨父体去重、逐名次销量、集中度三分、新品留存/推广。"""
from research_workflow.competition import build_competition_metrics
from research_workflow.data import MonthlyBSRConfig, build_monthly_top
from research_workflow.data.models import BSRRecord
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products


def _row(asin, sales, rating, count, parent="", brand="B", title="T"):
    return {"ASIN": asin, "父ASIN": parent, "品牌": brand, "商品标题": title,
            "月销量": sales, "评分": rating, "评分数": count}


def test_cross_parent_dedupe_uses_sales_rating_and_rating_count():
    rows = [
        _row("A1", 300, 4.5, 1200, parent="P1", title="Ice Maker"),
        _row("A2", 300, 4.5, 1200, parent="P2", title="Totally Different Title", brand="Other"),
        _row("A3", 300, 4.5, 1201, parent="P3", title="Ice Maker"),  # 评分数不同 => 不是重复
    ]
    result = build_monthly_top(rows, "2026-04")
    assert sorted(r.asin for r in result.records) == ["A1", "A3"]
    assert result.quality.duplicate_metric_rows_removed == 1
    assert result.dedupe_audit == [{
        "rule": "same_sales_rating_rating_count", "monthly_sales": 300, "rating": 4.5,
        "rating_count": 1200, "kept_asin": "A1", "removed_asins": ["A2"],
    }]


def test_same_brand_and_title_is_no_longer_treated_as_duplicate():
    rows = [_row("A1", 300, 4.5, 10, title="Same"), _row("A2", 250, 4.1, 50, title="Same")]
    assert len(build_monthly_top(rows, "2026-04").records) == 2


def test_missing_rating_is_not_judged_duplicate():
    rows = [{"ASIN": "A1", "月销量": 100}, {"ASIN": "A2", "月销量": 100},
            _row("A3", 100, 4.0, 0), _row("A4", 100, 4.0, 0)]
    result = build_monthly_top(rows, "2026-04", MonthlyBSRConfig())
    assert len(result.records) == 4
    assert result.quality.dedupe_unjudgeable_rows == 4


def _rec(asin, month, sales, rank, brand="", seller=""):
    return BSRRecord(asin, month, sales, brand=brand or asin, node_rank=rank, seller=seller)


def test_rank_points_are_per_rank_not_cumulative():
    rows = [_rec(f"A{i}", "2026-01", 100 - i, i) for i in range(1, 6)]
    rows += [_rec(f"A{i}", "2025-01", 50 - i, i) for i in range(1, 6)]
    points = {(p.month, p.rank): p for p in build_competition_metrics(rows, rank_points=(1, 2, 5, 10)).rank_points}
    assert points[("2026-01", 1)].sales == 99
    assert points[("2026-01", 2)].sales == 98          # 第2名自身，不是 99+98
    assert points[("2026-01", 5)].sales == 95
    assert points[("2026-01", 10)].sales is None       # 没有第10名 => 未知而非 0
    assert points[("2026-01", 2)].yoy_pct == (98 / 48 - 1) * 100


def test_concentration_separates_seller_asin_and_brand():
    rows = [_rec("A1", "2026-01", 60, 1, brand="X", seller="S1"),
            _rec("A2", "2026-01", 30, 2, brand="X", seller="S2"),
            _rec("A3", "2026-01", 10, 3, brand="Y", seller="S2")]
    c = build_competition_metrics(rows).concentration[0]
    assert c.top3_asin_share_pct == 100.0
    assert c.top3_brand_share_pct == 100.0
    assert c.top3_seller_share_pct == 100.0
    c2 = build_competition_metrics([_rec("A1", "2026-01", 60, 1, brand="X"),
                                    _rec("A2", "2026-01", 40, 2, brand="X")]).concentration[0]
    assert c2.top3_seller_share_pct is None and c2.seller_coverage_pct == 0.0
    assert c2.top3_brand_share_pct == 100.0


def _snaps(asin, sales_by_month, launch=None):
    return [NewProductSnapshot(asin, m, s, launch_date=launch) for m, s in sales_by_month.items()]


def test_retention_rate_uses_all_valid_new_products_as_denominator():
    snaps = []
    snaps += _snaps("OK", {"2026-01": 300, "2026-02": 300, "2026-03": 300, "2026-04": 300})
    snaps += _snaps("DIED", {"2026-01": 300, "2026-02": 300, "2026-03": 300, "2026-04": 10})
    snaps += _snaps("NEVER", {"2026-01": 10, "2026-02": 10, "2026-03": 10, "2026-04": 10})
    snaps += _snaps("GONE", {"2026-01": 300, "2026-02": 300, "2026-03": 300})  # 4 月未观测
    s = analyze_new_products(snaps, NewProductConfig()).summary
    assert s.total_products == 4
    assert s.retained_products == 1
    assert s.retention_unknown_products == 1
    assert s.retention_rate_pct == 25.0
    assert s.retention_rate_upper_pct == 50.0
    # 旧指标仍存在，但口径不同：分母是已观测的历史成功者
    assert s.survival_rate_among_successful_pct == 50.0


def test_promotion_uses_separate_threshold_over_first_three_months():
    cfg = NewProductConfig(promotion_threshold=500)
    snaps = []
    snaps += _snaps("FAST", {"2026-01": 100, "2026-02": 600, "2026-03": 100, "2026-06": 0}, launch="2026-01-15")
    snaps += _snaps("SLOW", {"2026-01": 100, "2026-02": 100, "2026-03": 100, "2026-06": 900}, launch="2026-01-20")
    snaps += _snaps("GAP", {"2026-01": 100, "2026-03": 100, "2026-06": 0}, launch="2026-01-02")  # 2 月缺失
    r = analyze_new_products(snaps, cfg)
    by = {o.asin: o for o in r.outcomes}
    assert by["FAST"].promoted is True and by["FAST"].promotion_month == "2026-02"
    assert by["SLOW"].promoted is False            # 第 6 个月达标不算“首三个月”
    assert by["GAP"].promoted is None              # 缺月 => 未知
    assert r.summary.promotion_rate_pct == (1 / 3) * 100
    assert r.summary.promotion_rate_upper_pct == (2 / 3) * 100


def test_promotion_disabled_without_threshold():
    r = analyze_new_products(_snaps("A", {"2026-01": 999, "2026-02": 999, "2026-03": 999}), NewProductConfig())
    assert r.summary.promotion_rate_pct is None and r.outcomes[0].promoted is None


def test_rank_point_is_nth_position_not_raw_node_rank():
    """已确认口径：按位次取第 N 名。名次有空缺/重复（去重、多节点合并）时仍按排序后的位次。"""
    rows = [_rec("A", "2026-01", 100, 1), _rec("C", "2026-01", 70, 3), _rec("D", "2026-01", 60, 3)]
    pts = {p.rank: p for p in build_competition_metrics(rows, rank_points=(1, 2, 3, 4)).rank_points}
    assert (pts[1].asin, pts[1].sales) == ("A", 100)
    assert (pts[2].asin, pts[2].sales) == ("C", 70)     # 原始排名 3 的商品，位次是第 2
    assert (pts[3].asin, pts[3].sales) == ("D", 60)     # 重复排名按销量降序稳定排位
    assert pts[4].asin is None and pts[4].sales is None  # 不存在的位次是未知，不是 0


def test_retention_numerator_does_not_require_historical_launch_success():
    """已确认口径：留存只看当期是否达标，不要求历史连续达标。"""
    snaps = _snaps("LATE", {"2026-04": 300})                                      # 只出现一个月：从未“连续达标”
    snaps += _snaps("ZIGZAG", {"2026-01": 300, "2026-02": 10, "2026-03": 300, "2026-04": 300})
    snaps += _snaps("LOW", {"2026-01": 300, "2026-02": 300, "2026-03": 300, "2026-04": 10})  # 曾达标但当期不达标
    r = analyze_new_products(snaps, NewProductConfig())
    by = {o.asin: o for o in r.outcomes}
    assert by["LATE"].launch_success is False and by["LATE"].retained is True
    assert by["ZIGZAG"].launch_success is False and by["ZIGZAG"].retained is True
    assert by["LOW"].launch_success is True and by["LOW"].retained is False
    assert r.summary.retained_products == 2 and r.summary.retention_rate_pct == (2 / 3) * 100
