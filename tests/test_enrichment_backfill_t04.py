from __future__ import annotations

import asyncio

from research_workflow.data.models import BSRRecord
from research_workflow.enrichment import (
    ASINEnrichmentService,
    ASINProfile,
    enrich_bullets_unique_asins,
    enrich_missing_regions,
    profile_from_payload,
)


class FakeProvider:
    def __init__(self, name, payloads):
        self.name = name
        self.payloads = payloads
        self.calls = []

    async def get_product_detail(self, asin, marketplace):
        self.calls.append(asin)
        value = self.payloads.get(asin)
        if isinstance(value, Exception):
            raise value
        return value


def service(primary_payloads, fallback_payloads=None):
    primary = FakeProvider("primary", primary_payloads)
    fallback = FakeProvider("fallback", fallback_payloads or {})
    return ASINEnrichmentService(primary, [fallback]), primary, fallback


# ---- T1-6 seller_region -------------------------------------------------


def test_vc_is_account_type_not_seller_region():
    profile = profile_from_payload(
        {"asin": "A1", "title": "T", "sellerRegion": "VC"}, asin="A1", source="s"
    )
    assert profile.seller_region == "UNKNOWN"
    assert profile.attributes["account_type"] == "VC"


def test_only_missing_or_unknown_regions_are_looked_up_and_cn_hk_merges():
    svc, primary, fallback = service(
        {
            "A2": {"asin": "A2", "sellerRegion": "CN(HK)"},
            "A3": {"asin": "A3", "sellerRegion": "VC"},
        },
        {"A3": {"asin": "A3", "sellerRegion": "US"}},
    )
    profiles = [
        ASINProfile(asin="A1", seller_region="US"),
        ASINProfile(asin="A2"),
        ASINProfile(asin="A3", seller_region="VC"),
        ASINProfile(asin="A4", seller_region="CN(HK)"),
    ]
    report = asyncio.run(enrich_missing_regions(profiles, svc))

    assert sorted(primary.calls) == ["A2", "A3"]  # A1/A4 already known: not queried
    assert report.profiles["A1"].seller_region == "US"
    assert report.profiles["A2"].seller_region == "CN"
    assert report.profiles["A3"].seller_region == "US"
    assert report.profiles["A3"].attributes["account_type"] == "VC"
    assert report.profiles["A4"].seller_region == "CN"
    assert (report.total_profiles, report.targeted, report.filled) == (4, 2, 2)
    assert report.unknown_before == 2 and report.unknown_after == 0
    assert report.unknown_ratio_before == 0.5 and report.unknown_ratio_after == 0.0
    assert report.conflicts == []
    # inputs not mutated
    assert profiles[1].seller_region == "UNKNOWN"


def test_region_conflict_is_recorded_and_primary_value_kept():
    svc, _, _ = service(
        {"A1": {"asin": "A1", "sellerRegion": "CN"}},
        {"A1": {"asin": "A1", "sellerRegion": "US"}},
    )
    report = asyncio.run(enrich_missing_regions([ASINProfile(asin="A1")], svc))
    assert report.profiles["A1"].seller_region == "CN"
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert (c.asin, c.field, c.chosen) == ("A1", "seller_region", "CN")
    assert c.values == {"primary": "CN", "fallback": "US"}


def test_region_lookup_failure_is_recorded_and_stays_unknown():
    svc, _, _ = service({"A1": RuntimeError("boom")})
    report = asyncio.run(enrich_missing_regions([ASINProfile(asin="A1")], svc, attempts=2))
    assert report.profiles["A1"].seller_region == "UNKNOWN"
    assert report.unknown_after == 1
    assert [f.source for f in report.failures] == ["primary"]


def test_enrichment_falls_back_when_primary_lacks_seller_region():
    svc, primary, fallback = service(
        {"A1": {"asin": "A1", "title": "T", "brand": "B", "features": ["f"]}},
        {"A1": {"asin": "A1", "sellerRegion": "US"}},
    )
    result = asyncio.run(svc.enrich(["A1"]))
    assert fallback.calls == ["A1"]  # title/brand/bullets complete, region missing -> fallback
    assert result.profiles["A1"].seller_region == "US"


# ---- T1-7 bullets -------------------------------------------------------


def test_same_asin_in_three_months_is_fetched_once_and_backfilled_everywhere():
    svc, primary, _ = service(
        {
            "A1": {"asin": "A1", "features": ["Fast ice"]},
            "A2": {"asin": "A2", "features": ["Quiet"]},
        }
    )
    months = {
        "2025-01": [BSRRecord(asin="A1", month="2025-01", monthly_sales=10)],
        "2025-02": [
            BSRRecord(asin="A1", month="2025-02", monthly_sales=11),
            BSRRecord(asin="A2", month="2025-02", monthly_sales=5),
        ],
        "2025-03": [{"asin": "a1", "month": "2025-03"}],
    }
    result = asyncio.run(enrich_bullets_unique_asins(months, svc))

    assert sorted(primary.calls) == ["A1", "A2"]
    assert result.unique_asins == 2 and result.fetched_asins == 2
    for month in ("2025-01", "2025-02", "2025-03"):
        assert result.filled_records[month][0]["bullets"] == ["Fast ice"]
    assert result.filled_records["2025-02"][1]["bullets"] == ["Quiet"]
    assert result.missing_asins == [] and result.failures == []


def test_bullet_failures_missing_and_conflicts_are_reported():
    svc, _, _ = service(
        {
            "A1": RuntimeError("down"),
            "A2": {"asin": "A2", "features": ["Fetched"]},
        }
    )
    months = {
        "2025-01": [{"asin": "A1"}, {"asin": "A2"}],
        "2025-02": [{"asin": "A2", "bullets": ["Existing"]}],
    }
    result = asyncio.run(enrich_bullets_unique_asins(months, svc, attempts=1))

    assert result.missing_asins == ["A1"]
    assert {f.source for f in result.failures} == {"primary"}
    assert "bullets" not in result.filled_records["2025-01"][0]
    assert result.filled_records["2025-02"][0]["bullets"] == ["Existing"]  # not overwritten
    assert [(c.asin, c.field, c.month, c.chosen) for c in result.conflicts] == [
        ("A2", "bullets", "2025-02", "existing")
    ]


def test_bullets_accepts_plain_asin_list():
    svc, primary, _ = service({"A1": {"asin": "A1", "features": ["x"]}})
    result = asyncio.run(enrich_bullets_unique_asins(["A1", "a1"], svc))
    assert primary.calls == ["A1"]
    assert result.bullets_by_asin == {"A1": ["x"]}
