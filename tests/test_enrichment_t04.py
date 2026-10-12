from __future__ import annotations

import asyncio

from research_workflow.enrichment import ASINEnrichmentService, profile_from_payload


class FakeProvider:
    def __init__(self, name, payloads):
        self.name = name
        self.payloads = payloads
        self.calls = []

    async def get_product_detail(self, asin, marketplace):
        self.calls.append((asin, marketplace))
        value = self.payloads.get(asin)
        if isinstance(value, Exception):
            raise value
        return value


def test_profile_adapter_unwraps_common_envelope_and_aliases():
    payload = {
        "data": {
            "asin": "B012345678",
            "productTitle": "Countertop Ice Maker",
            "brandName": "ACME",
            "parentAsin": "B0PARENT00",
            "bulletPoints": ["Fast ice", "Self cleaning"],
            "sellerCountry": "CN(HK)",
            "currentPrice": "$89.99",
            "stars": "4.5",
            "ratings": "1,234",
        }
    }
    profile = profile_from_payload(payload, asin="B012345678", source="test")
    assert profile is not None
    assert profile.title == "Countertop Ice Maker"
    assert profile.bullets == ["Fast ice", "Self cleaning"]
    assert profile.seller_region == "CN"
    assert profile.price == 89.99
    assert profile.rating == 4.5
    assert profile.review_count == 1234
    assert profile.sources == ["test"]


def test_enrichment_dedupes_asins_and_fallback_only_fills_missing_fields():
    primary = FakeProvider(
        "sellersprite",
        {
            "A1": {"asin": "A1", "title": "Primary title", "brand": "BrandA"},
            "A2": RuntimeError("temporary failure"),
        },
    )
    fallback = FakeProvider(
        "sif",
        {
            "A1": {
                "asin": "A1",
                "title": "Fallback title",
                "brand": "WrongBrand",
                "features": ["Feature 1"],
                "sellerRegion": "US",
            },
            "A2": {
                "asin": "A2",
                "title": "Fallback product",
                "brand": "BrandB",
                "features": ["Feature 2"],
            },
        },
    )

    result = asyncio.run(
        ASINEnrichmentService(primary, [fallback], concurrency=2).enrich(
            ["a1", "A1", "a2"], "US"
        )
    )

    assert result.stats.requested_asins == 3
    assert result.stats.unique_asins == 2
    assert result.stats.primary_hits == 1
    assert result.stats.fallback_hits == 2
    assert result.stats.missing_profiles == 0
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 2

    a1 = result.profiles["A1"]
    assert a1.title == "Primary title"
    assert a1.brand == "BrandA"
    assert a1.bullets == ["Feature 1"]
    assert a1.seller_region == "US"
    assert a1.sources == ["sellersprite", "sif"]

    a2 = result.profiles["A2"]
    assert a2.title == "Fallback product"
    assert a2.sources == ["sif"]


def test_enrichment_can_leave_unavailable_asin_missing_without_crashing():
    provider = FakeProvider("sellersprite", {"A1": None})
    result = asyncio.run(ASINEnrichmentService(provider).enrich(["A1"]))
    assert result.profiles == {}
    assert result.stats.missing_profiles == 1
