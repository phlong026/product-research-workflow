from research_workflow.data import MonthlyBSRConfig, build_monthly_top, normalize_seller_region


def test_parent_and_title_dedup_then_topn():
    # 第二级去重默认已迁移为"月销量+评分+评分数三值相同"(见 test_bsr_cleaning_t14.py);
    # 本用例验证的是品牌+标题 legacy 口径,故显式选择 brand_title。
    rows = [
        {"ASIN": "A1", "父ASIN": "P1", "品牌": "BrandA", "商品标题": "Same Product", "月销量": "100", "小类BSR": "10", "卖家所属地": "CN(HK)"},
        {"ASIN": "A2", "父ASIN": "P1", "品牌": "BrandA", "商品标题": "Variant", "月销量": "200", "小类BSR": "8", "卖家所属地": "CN"},
        {"ASIN": "A3", "父ASIN": "", "品牌": "BrandB", "商品标题": "Duplicate Title", "月销量": "300", "小类BSR": "4", "卖家所属地": "US"},
        {"ASIN": "A4", "父ASIN": "", "品牌": "BrandB", "商品标题": "Duplicate   Title", "月销量": "250", "小类BSR": "5", "卖家所属地": "US"},
        {"ASIN": "A5", "父ASIN": "", "品牌": "BrandC", "商品标题": "Other", "月销量": "400", "小类BSR": "1", "卖家所属地": "VC"},
    ]
    result = build_monthly_top(rows, "2026-04", MonthlyBSRConfig(top_n=3, cross_parent_rule="brand_title"))
    assert [r.asin for r in result.records] == ["A5", "A3", "A2"]
    assert result.stats.input_rows == 5
    assert result.stats.parent_deduped_rows == 4
    assert result.stats.suspicious_deduped_rows == 3
    assert result.records[-1].seller_region == "CN"


def test_cn_hk_merge_is_configurable():
    assert normalize_seller_region("CN(HK)") == "CN"
    assert normalize_seller_region("CN(HK)", merge_cn_hk=False) == "CN(HK)"


def test_unknown_or_bad_sales_are_safe():
    result = build_monthly_top(
        [{"ASIN": "B1", "月销量": "n/a", "商品标题": "X"}, {"ASIN": "", "月销量": 10}],
        "2026-04",
    )
    assert not result.records
    assert result.quality.invalid_sales_rows == 1


def test_data_quality_report_exposes_input_problems_and_dedupe_counts():
    rows = [
        {"ASIN": "", "月销量": 10},
        {"ASIN": "A1", "父ASIN": "P1", "商品标题": "Same", "月销量": "bad", "品牌": "", "卖家所属地": "", "小类BSR": ""},
        {"ASIN": "A2", "父ASIN": "P1", "商品标题": "Variant", "月销量": 20, "品牌": "B"},
    ]
    result = build_monthly_top(rows, "2026-04")
    q = result.quality
    assert q is not None
    assert q.missing_asin_rows == 1
    assert q.invalid_sales_rows == 1
    assert q.missing_brand_rows == 0
    assert q.unknown_seller_region_rows == 1
    assert q.missing_rank_rows == 1
    assert q.duplicate_parent_rows_removed == 0
