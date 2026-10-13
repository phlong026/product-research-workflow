from __future__ import annotations

from datetime import date

import pytest

from research_workflow.competition_external.big_brand import (
    BrandAsin,
    check_big_brand,
    load_brand_asins,
)
from research_workflow.competition_external.ledger import EvidenceRef

EV = EvidenceRef("ABA export", "2026-10-01", file_path="aba.csv")


def _asins():
    return [
        BrandAsin("B01", "2022-05-10", 4.5, 300),
        BrandAsin("B02", "2020-01-15", 4.2, 200),
        BrandAsin("B03", "2021-03-01", 4.0, 100),
    ]


def test_aba_threshold_boundaries():
    at = check_big_brand("Acme", "ice maker", 20000, _asins(), EV)
    over = check_big_brand("Acme", "ice maker", 20001, _asins(), EV)
    under = check_big_brand("Acme", "ice maker", 1, _asins(), EV)
    assert at.status == "candidate_big_brand"  # <= is inclusive
    assert over.status == "not_candidate"
    assert under.status == "candidate_big_brand"


def test_aba_missing_is_unverified_and_never_judged():
    result = check_big_brand("Acme", "ice maker", None, _asins(), EV)
    assert result.status == "unverified"
    assert any("ABA 排名缺失" in note for note in result.notes)
    # ASIN summary is still provided
    assert result.asin_count == 3


def test_threshold_is_configurable():
    assert check_big_brand("Acme", "ice maker", 5000, _asins(), EV, threshold=5000).status == "candidate_big_brand"
    assert check_big_brand("Acme", "ice maker", 5001, _asins(), EV, threshold=5000).status == "not_candidate"
    with pytest.raises(ValueError):
        check_big_brand("Acme", "ice maker", 10, _asins(), EV, threshold=0)
    with pytest.raises(ValueError):
        check_big_brand("Acme", "ice maker", 0, _asins(), EV)


def test_summary_and_earliest_listing():
    result = check_big_brand("Acme", "ice maker", 100, _asins(), EV)
    assert result.asin_count == 3
    assert result.total_monthly_sales == 600
    assert result.earliest_asin == "B02"
    assert result.earliest_listed_date == date(2020, 1, 15)
    assert result.excluded_date_count == 0
    assert result.human_interpretation_slot == ""
    assert result.to_dict()["evidence"]["source_name"] == "ABA export"


def test_invalid_dates_are_excluded_and_counted():
    asins = _asins() + [
        BrandAsin("B04", "", None, 50),  # empty
        BrandAsin("B05", "not-a-date", None, None),  # unparsable
        BrandAsin("B06", "1970-01-01", None, 10),  # impossible (before Amazon)
        BrandAsin("B07", "2027-01-01", None, 10),  # after retrieval date
    ]
    result = check_big_brand("Acme", "ice maker", 100, asins, EV)
    assert result.earliest_asin == "B02"
    assert result.excluded_date_count == 4
    assert result.asin_count == 7
    assert result.sales_missing_count == 1  # B05
    assert result.total_monthly_sales == 670
    assert any("4 个 ASIN" in note for note in result.notes)


def test_no_valid_date_gives_none():
    result = check_big_brand("Acme", "ice maker", 100, [BrandAsin("B01", "bad")], EV)
    assert result.earliest_listed_date is None and result.earliest_asin is None
    assert result.total_monthly_sales is None


def test_duplicate_asin_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        check_big_brand("Acme", "ice maker", 100, [BrandAsin("B01"), BrandAsin("B01")], EV)


def test_load_brand_asins_csv(tmp_path):
    path = tmp_path / "asins.csv"
    path.write_text(
        "asin,listed_date,rating,monthly_sales\nB01,2022-05-10,4.5,300\nB02,,,\n", encoding="utf-8"
    )
    asins = load_brand_asins(path)
    assert asins[0].monthly_sales == 300 and asins[1].monthly_sales is None
    with pytest.raises(ValueError, match="row 1"):
        bad = tmp_path / "bad.csv"
        bad.write_text("asin,listed_date\n,2022-01-01\n", encoding="utf-8")
        load_brand_asins(bad)
