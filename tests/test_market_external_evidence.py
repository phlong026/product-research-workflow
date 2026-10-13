import json

import pytest

from research_workflow.market_external import (
    EvidenceLedger,
    ExternalEvidence,
    check_trends_consistency,
    combine_for_chart,
    import_report_excerpts,
    parse_report_excerpts,
    parse_trends_csv,
    trends_to_evidence,
)


def _ev(**over):
    base = dict(
        claim_id="c1", metric="units", value=100.0, unit="units", period="2025",
        source_name="vendor", source_url="file://x.csv", as_of="2026-01-05", retrieved_from="file",
    )
    base.update(over)
    return ExternalEvidence(**base)


def test_require_sources_passes_with_complete_sources():
    ledger = EvidenceLedger()
    ledger.add(_ev())
    ledger.require_sources()


@pytest.mark.parametrize("field", ["source_name", "source_url", "as_of"])
def test_require_sources_rejects_present_number_without_source(field):
    ledger = EvidenceLedger()
    ledger.add(_ev(**{field: ""}))
    with pytest.raises(ValueError, match=field):
        ledger.require_sources()


def test_missing_evidence_is_explicit_and_listed():
    ledger = EvidenceLedger()
    ledger.add(_ev())
    ledger.add(_ev(claim_id="c2", value=None, status="missing", source_name="", source_url="", as_of=""))
    assert [e.claim_id for e in ledger.missing()] == ["c2"]
    ledger.require_sources()  # missing rows need no source
    assert ledger.to_dict()["missing_claim_ids"] == ["c2"]


def test_evidence_validation():
    with pytest.raises(ValueError):
        _ev(value=None)  # present without value
    with pytest.raises(ValueError):
        _ev(status="missing")  # missing but carries a value
    with pytest.raises(ValueError):
        _ev(retrieved_from="scrape")
    with pytest.raises(ValueError):
        _ev(as_of="yesterday")
    ledger = EvidenceLedger()
    ledger.add(_ev())
    with pytest.raises(ValueError):
        ledger.add(_ev())


TRENDS = "date,keyword,value\n" + "\n".join(
    [
        "2024-01-07,ice maker,40", "2024-01-14,ice maker,60",
        "2025-01-05,ice maker,75", "2025-02-02,ice maker,50",
        "2025-01-05,nugget ice,10",
    ]
)


def _export(text=TRENDS, **over):
    kw = dict(geo="US", timeframe="2024-01-01 2025-12-31", source_name="Google Trends",
              source_url="file://trends.csv", as_of="2026-01-10")
    kw.update(over)
    return parse_trends_csv(text, **kw)


def test_trends_monthly_mean_and_yoy_hand_calc():
    export = _export()
    ice = next(s for s in export.series if s.keyword == "ice maker")
    assert dict(ice.monthly) == {"2024-01": 50.0, "2025-01": 75.0, "2025-02": 50.0}
    yoy = dict(ice.yoy)
    assert yoy["2025-01"] == pytest.approx(75 / 50 - 1)
    assert yoy["2024-01"] is None and yoy["2025-02"] is None
    assert set(export.keywords) == {"ice maker", "nugget ice"}


def test_trends_declared_keywords_must_match_and_values_numeric():
    with pytest.raises(ValueError, match="keywords"):
        _export(keywords=["ice maker"])
    with pytest.raises(ValueError, match="non-numeric"):
        _export("date,keyword,value\n2025-01-01,a,<1")


def test_trends_inconsistent_scope_reported_and_refused():
    a, b = _export(), _export(geo="GB", timeframe="2020-01-01 2021-01-01")
    mismatches = check_trends_consistency([a, b])
    assert any("geo" in m for m in mismatches) and any("timeframe" in m for m in mismatches)
    with pytest.raises(ValueError, match="inconsistent"):
        combine_for_chart([a, b])
    assert len(combine_for_chart([a, _export()])) == 4


def test_trends_to_evidence_carries_source():
    ledger = EvidenceLedger()
    for e in trends_to_evidence(_export()):
        ledger.add(e)
    ledger.require_sources()
    assert "trends:US:ice maker:2025-01:yoy" in ledger


def test_report_excerpt_inaccessible_registered(tmp_path):
    rows = [
        {"excerpt": "US countertop ice makers grew", "source_name": "Report A", "date": "2025-12-01", "url": "https://a.example/r"},
        {"source_name": "Paywalled B", "date": "2025-11-01", "url": "https://b.example/r", "status": "inaccessible"},
    ]
    p = tmp_path / "ex.json"
    p.write_text(json.dumps({"excerpts": rows}), encoding="utf-8")
    items = import_report_excerpts(p)
    assert [i.status for i in items] == ["present", "inaccessible"]
    assert items[1].value is None
    ledger = EvidenceLedger()
    for i in items:
        ledger.add(i)
    ledger.require_sources()
    assert [e.claim_id for e in ledger.missing()] == ["report:2"]


def test_report_excerpt_without_url_fails_source_check_and_csv_import(tmp_path):
    p = tmp_path / "ex.csv"
    p.write_text("excerpt,source_name,date,url\nsome claim,Report C,2025-10-01,\n", encoding="utf-8")
    ledger = EvidenceLedger()
    for i in import_report_excerpts(p):
        ledger.add(i)
    with pytest.raises(ValueError, match="source_url"):
        ledger.require_sources()
    with pytest.raises(ValueError):
        parse_report_excerpts([{"excerpt": "x", "status": "bogus"}])
