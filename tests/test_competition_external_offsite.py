from __future__ import annotations

import pytest

from research_workflow.competition_external.ledger import EvidenceRef
from research_workflow.competition_external.offsite import (
    NOT_FOUND_NOTE,
    CrossCategoryAsin,
    OffsiteFinding,
    OffsiteLedger,
    OffsiteSearch,
    load_cross_category_asins,
    load_offsite_searches,
    summarize_cross_category,
)

EV = EvidenceRef("Seller export", "2026-10-01", file_path="brand_asins.csv")


def _search(status, **kw):
    base = dict(brand="Acme", product_term="ice maker", query="Acme ice maker review",
                engine_tool="Google", status=status, searched_at="2026-10-01")
    base.update(kw)
    return OffsiteSearch(**base)


def test_not_found_note_says_not_found_is_not_nonexistence():
    result = _search("not_found").to_dict()
    assert "未找到≠不存在" in result["notes"][0]
    assert result["status"] == "not_found"
    assert "未找到≠不存在" in NOT_FOUND_NOTE


def test_three_statuses_are_distinct_and_validated():
    found = _search("found", findings=(OffsiteFinding("social", "https://t.co/x", "2026-09-30"),))
    unsearched = _search("not_searched", searched_at=None)
    assert {found.to_dict()["status"], _search("not_found").to_dict()["status"],
            unsearched.to_dict()["status"]} == {"found", "not_found", "not_searched"}
    assert "未检索" in unsearched.to_dict()["notes"][0]
    with pytest.raises(ValueError):
        _search("found")  # found without findings
    with pytest.raises(ValueError):
        _search("not_found", findings=(OffsiteFinding("social", "https://x", "2026-09-30"),))
    with pytest.raises(ValueError):
        _search("not_found", searched_at=None)
    with pytest.raises(ValueError):
        _search("unknown")
    with pytest.raises(ValueError):
        _search("not_found", engine_tool="")


def test_cross_category_summary_facts_only():
    asins = [
        CrossCategoryAsin("A1", "ice maker", "2021-03-01", 300),
        CrossCategoryAsin("A2", "ice maker", "2024-06-01", 0),
        CrossCategoryAsin("A3", "blender", "2025-01-01", 100),
        CrossCategoryAsin("A4", "blender", "bad", None),
    ]
    summary = summarize_cross_category(asins, EV)
    assert summary["category_count"] == 2 and summary["asin_count"] == 4
    blender, ice = summary["categories"]
    assert ice["category"] == "ice maker"
    assert ice["asin_count"] == 2 and ice["total_monthly_sales"] == 300 and ice["asins_with_sales"] == 1
    assert ice["earliest_listed_date"] == "2021-03-01" and ice["latest_listed_date"] == "2024-06-01"
    assert ice["listings_by_year"] == {"2021": 1, "2024": 1}
    assert blender["excluded_date_count"] == 1 and blender["sales_missing_count"] == 1
    # no maturity labels are produced
    assert not any(k in blender for k in ("maturity", "status", "label"))


def test_ledger_leaves_human_slot_and_carries_notes():
    ledger = OffsiteLedger(
        "Acme",
        (_search("not_found"), _search("not_searched", searched_at=None, engine_tool="Bing")),
        (CrossCategoryAsin("A1", "ice maker", "2021-03-01", 300),),
        EV,
    )
    out = ledger.to_dict()
    assert out["human_judgement_slot"] == ""
    assert out["status_counts"] == {"found": 0, "not_found": 1, "not_searched": 1}
    assert any("未找到≠不存在" in n for n in out["notes"])
    assert any("人工判断" in n for n in out["notes"])


def test_ledger_validations():
    with pytest.raises(ValueError, match="does not match"):
        OffsiteLedger("Other", (_search("not_found"),))
    with pytest.raises(ValueError, match="cross_category_evidence"):
        OffsiteLedger("Acme", (), (CrossCategoryAsin("A1", "x"),))
    with pytest.raises(ValueError, match="duplicate"):
        summarize_cross_category([CrossCategoryAsin("A1", "x"), CrossCategoryAsin("A1", "y")], EV)


def test_import_csvs(tmp_path):
    searches = tmp_path / "s.csv"
    searches.write_text(
        "brand,product_term,query,engine_tool,searched_at,status,channel_type,url,discovered_at\n"
        "Acme,ice maker,Acme ice maker,Google,2026-10-01,found,social,https://t.co/1,2026-09-30\n"
        "Acme,ice maker,Acme ice maker,Google,2026-10-01,found,deal site,https://d.com/2,2026-09-29\n"
        "Acme,ice maker,Acme coupon,Google,2026-10-01,not_found,,,\n",
        encoding="utf-8",
    )
    loaded = load_offsite_searches(searches)
    assert len(loaded) == 2
    assert len(loaded[0].findings) == 2 and loaded[1].status == "not_found"

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "brand,product_term,query,engine_tool,searched_at,status\nAcme,ice maker,q,,2026-10-01,not_found\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="engine_tool"):
        load_offsite_searches(bad)

    asin_file = tmp_path / "a.csv"
    asin_file.write_text(
        "asin,category,listed_date,monthly_sales\nA1,ice maker,2021-03-01,300\n", encoding="utf-8"
    )
    assert load_cross_category_asins(asin_file)[0].monthly_sales == 300
