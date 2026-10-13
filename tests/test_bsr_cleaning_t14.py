import json

from research_workflow.data import MonthlyBSRConfig, build_monthly_top, parse_bsr_row

MONTH = "2026-04"


def _row(asin, sales, *, parent="", brand="B", title="", rating="4.5", count="100", rank=None, **extra):
    row = {"ASIN": asin, "月销量": sales, "父ASIN": parent, "品牌": brand, "商品标题": title}
    if rating is not None:
        row["评分"] = rating
    if count is not None:
        row["评分数"] = count
    if rank is not None:
        row["小类BSR"] = rank
    row.update(extra)
    return row


# ---------- T1-4 别名 ----------
def test_alias_fields_are_parsed():
    record = parse_bsr_row(
        {"ASIN": "a1", "月销量": 10, "卖家ID": "S1", "评分": "4.6", "评分数": "1,234"}, MONTH
    )
    assert (record.seller_id, record.rating, record.rating_count) == ("S1", 4.6, 1234)

    record = parse_bsr_row(
        {"asin": "a2", "monthly_sales": 10, "sellerId": "S2", "Rating": 4.2, "ratingCount": 55}, MONTH
    )
    assert (record.seller_id, record.rating, record.rating_count) == ("S2", 4.2, 55)

    record = parse_bsr_row(
        {"asin": "a3", "monthly_sales": 10, "Seller ID": "S3", "星级": "3.9", "reviews": "7"}, MONTH
    )
    assert (record.seller_id, record.rating, record.rating_count) == ("S3", 3.9, 7)


def test_missing_or_invalid_rating_fields_are_none():
    record = parse_bsr_row({"ASIN": "a1", "月销量": 10, "评分": "n/a", "评分数": ""}, MONTH)
    assert record.rating is None and record.rating_count is None
    # lowercase "ratings" carrying a review count must not be misread as a star rating
    record = parse_bsr_row({"ASIN": "a2", "月销量": 10, "ratings": "1234"}, MONTH)
    assert record.rating is None
    assert record.rating_count == 1234


# ---------- T1-4 去重 4 组反例 ----------
def test_same_parent_collapses_via_parent_rule():
    rows = [
        _row("A1", 100, parent="P1", title="x"),
        _row("A2", 100, parent="P1", title="y", rating="4.1", count="9"),
    ]
    result = build_monthly_top(rows, MONTH)
    assert [r.asin for r in result.records] == ["A1"]
    assert [(e.rule, e.kept_asin, e.removed_asin) for e in result.dedupe_audit] == [
        ("parent_asin", "A1", "A2")
    ]


def test_different_parent_same_triple_is_deduped_and_audited():
    rows = [
        _row("A1", 100, parent="P1", title="one", rank=5),
        _row("A2", 100, parent="P2", title="two", rank=3),
        _row("A3", 90, parent="P3", title="three"),
    ]
    result = build_monthly_top(rows, MONTH)
    # same sales: better node_rank wins as representative
    assert [r.asin for r in result.records] == ["A2", "A3"]
    assert result.quality.duplicate_title_rows_removed == 1
    assert result.stats.suspicious_deduped_rows == 2
    (entry,) = result.dedupe_audit
    assert (entry.month, entry.rule, entry.kept_asin, entry.removed_asin) == (
        MONTH, "sales_rating_reviews", "A2", "A1",
    )
    assert entry.reason


def test_same_title_but_different_triple_is_not_removed():
    rows = [
        _row("A1", 100, parent="P1", brand="Acme", title="Same Title", rating="4.5", count="100"),
        _row("A2", 100, parent="P2", brand="Acme", title="Same Title", rating="4.4", count="100"),
        _row("A3", 100, parent="P3", brand="Acme", title="Same Title", rating="4.5", count="101"),
        _row("A4", 99, parent="P4", brand="Acme", title="Same Title", rating="4.5", count="100"),
    ]
    result = build_monthly_top(rows, MONTH)
    assert sorted(r.asin for r in result.records) == ["A1", "A2", "A3", "A4"]
    assert result.dedupe_audit == []


def test_missing_rating_or_count_never_dedupes():
    rows = [
        _row("A1", 100, parent="P1", rating=None, count="100"),
        _row("A2", 100, parent="P2", rating=None, count="100"),
        _row("A3", 100, parent="P3", rating="4.5", count=None),
        _row("A4", 100, parent="P4", rating="4.5", count=None),
        _row("A5", 100, parent="P5", rating=None, count=None),
        _row("A6", 100, parent="P6", rating=None, count=None),
    ]
    result = build_monthly_top(rows, MONTH)
    assert len(result.records) == 6
    assert result.dedupe_audit == []


def test_zero_rating_or_zero_count_is_not_evidence_of_duplicate():
    rows = [
        _row("A1", 10, parent="P1", rating="0", count="0"),
        _row("A2", 10, parent="P2", rating="0", count="0"),
    ]
    assert len(build_monthly_top(rows, MONTH).records) == 2


def test_triple_dedupe_is_input_order_independent():
    rows = [
        _row("A1", 100, parent="P1", rank=5),
        _row("A2", 100, parent="P2", rank=3),
        _row("A3", 100, parent="P3", rank=3),
    ]
    forward = build_monthly_top(rows, MONTH)
    backward = build_monthly_top(list(reversed(rows)), MONTH)
    assert [r.asin for r in forward.records] == [r.asin for r in backward.records] == ["A2"]
    assert forward.dedupe_audit == backward.dedupe_audit


def test_legacy_brand_title_rule_still_available():
    rows = [
        _row("A1", 300, brand="BrandB", title="Duplicate Title", rating="4.1", count="10"),
        _row("A2", 250, brand="BrandB", title="Duplicate   Title", rating="4.9", count="99"),
    ]
    legacy = build_monthly_top(rows, MONTH, MonthlyBSRConfig(cross_parent_rule="brand_title"))
    assert [r.asin for r in legacy.records] == ["A1"]
    assert legacy.dedupe_audit[0].rule == "brand_title"
    default = build_monthly_top(rows, MONTH)
    assert len(default.records) == 2


def test_none_rule_disables_cross_parent_dedupe():
    rows = [_row("A1", 100, parent="P1"), _row("A2", 100, parent="P2")]
    result = build_monthly_top(rows, MONTH, MonthlyBSRConfig(cross_parent_rule="none"))
    assert len(result.records) == 2


def test_invalid_rule_rejected():
    import pytest

    with pytest.raises(ValueError):
        build_monthly_top([], MONTH, MonthlyBSRConfig(cross_parent_rule="bogus"))  # type: ignore[arg-type]


def test_audit_is_json_serializable():
    rows = [_row("A1", 100, parent="P1"), _row("A2", 100, parent="P2", rank=1)]
    result = build_monthly_top(rows, MONTH)
    payload = [e.to_dict() for e in result.dedupe_audit]
    assert set(payload[0]) == {"month", "rule", "kept_asin", "removed_asin", "reason"}
    json.dumps(payload, ensure_ascii=False)


# ---------- T1-5 Top N 与名次 ----------
def test_sales_rank_is_continuous_and_node_rank_preserved():
    rows = [
        _row("A1", 50, rank=30, rating="4.0", count="1"),
        _row("A2", 300, rank=7, rating="4.1", count="2"),
        _row("A3", 120, rank=2, rating="4.2", count="3"),
        _row("A4", 80, rank=11, rating="4.3", count="4"),
    ]
    result = build_monthly_top(rows, MONTH)
    assert [r.asin for r in result.records] == ["A2", "A3", "A4", "A1"]
    assert [r.sales_rank for r in result.records] == [1, 2, 3, 4]
    assert [r.node_rank for r in result.records] == [7, 2, 11, 30]


def test_top_n_truncates_and_ranks_up_to_n():
    rows = [_row(f"A{i:03d}", 1000 - i, rating=str(3 + i / 1000), count=str(i + 1)) for i in range(150)]
    result = build_monthly_top(rows, MONTH)
    assert len(result.records) == 100
    assert [r.sales_rank for r in result.records] == list(range(1, 101))
    assert result.records[0].asin == "A000"


def test_fewer_than_n_is_not_padded():
    rows = [_row(f"A{i}", 10 + i, rating=str(4 + i / 10), count=str(i + 1)) for i in range(7)]
    result = build_monthly_top(rows, MONTH, MonthlyBSRConfig(top_n=100))
    assert len(result.records) == 7
    assert result.records[-1].sales_rank == 7
    assert result.stats.output_rows == 7


def test_tied_sales_use_node_rank_then_asin_for_stable_order():
    rows = [
        _row("A3", 100, rating="4.3", count="3", rank=5),
        _row("A2", 100, rating="4.2", count="2", rank=5),
        _row("A1", 100, rating="4.1", count="1", rank=9),
        _row("A0", 100, rating="4.0", count="0", rank=None),
    ]
    first = build_monthly_top(rows, MONTH)
    second = build_monthly_top(list(reversed(rows)), MONTH)
    expected = ["A2", "A3", "A1", "A0"]
    assert [r.asin for r in first.records] == expected
    assert [r.asin for r in second.records] == expected
    assert [r.sales_rank for r in first.records] == [1, 2, 3, 4]
