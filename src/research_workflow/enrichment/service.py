from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .adapters import merge_profiles, profile_from_payload
from .models import ASINProfile, EnrichmentResult, EnrichmentStats


class ProductDetailProvider(Protocol):
    name: str

    async def get_product_detail(self, asin: str, marketplace: str) -> Any: ...


class SellerSpriteProductDetailProvider:
    """Small adapter around SellerSpriteConnector.

    Operation names are kept configurable because the open-source SellerSprite MCP
    catalog can evolve independently of this project.
    """

    name = "sellersprite"

    def __init__(self, connector: Any, operation: str = "asin_detail") -> None:
        self.connector = connector
        self.operation = operation

    async def get_product_detail(self, asin: str, marketplace: str) -> Any:
        response = await self.connector.call_operation(
            self.operation,
            {"asin": asin, "marketplace": marketplace},
        )
        return response.data


class MCPToolProductDetailProvider:
    """Adapter for SIF/Xiyou/other generic MCP servers exposing a detail tool."""

    def __init__(self, connector: Any, *, name: str, tool: str) -> None:
        self.connector = connector
        self.name = name
        self.tool = tool

    async def get_product_detail(self, asin: str, marketplace: str) -> Any:
        response = await self.connector.call(
            self.tool,
            {"asin": asin, "site": marketplace, "marketplace": marketplace},
        )
        return response.data


class ASINEnrichmentService:
    def __init__(
        self,
        primary: ProductDetailProvider,
        fallbacks: list[ProductDetailProvider] | None = None,
        *,
        concurrency: int = 5,
    ) -> None:
        self.primary = primary
        self.fallbacks = fallbacks or []
        self.concurrency = max(1, int(concurrency))

    async def enrich(self, asins: list[str], marketplace: str = "US") -> EnrichmentResult:
        unique = list(dict.fromkeys(a.strip().upper() for a in asins if a and a.strip()))
        semaphore = asyncio.Semaphore(self.concurrency)

        primary_hits = 0
        fallback_hits = 0
        missing = 0

        async def one(asin: str) -> tuple[str, ASINProfile | None, bool, bool]:
            async with semaphore:
                profile: ASINProfile | None = None
                used_primary = False
                used_fallback = False

                try:
                    payload = await self.primary.get_product_detail(asin, marketplace)
                    profile = profile_from_payload(payload, asin=asin, source=self.primary.name)
                    used_primary = profile is not None
                except Exception:
                    profile = None

                for provider in self.fallbacks:
                    if profile is not None and _is_complete_enough(profile):
                        break
                    try:
                        payload = await provider.get_product_detail(asin, marketplace)
                        fallback_profile = profile_from_payload(
                            payload, asin=asin, source=provider.name
                        )
                    except Exception:
                        fallback_profile = None
                    if fallback_profile is None:
                        continue
                    used_fallback = True
                    profile = (
                        fallback_profile
                        if profile is None
                        else merge_profiles(profile, fallback_profile)
                    )
                return asin, profile, used_primary, used_fallback

        rows = await asyncio.gather(*(one(asin) for asin in unique))
        profiles: dict[str, ASINProfile] = {}
        for asin, profile, used_primary, used_fallback in rows:
            if used_primary:
                primary_hits += 1
            if used_fallback:
                fallback_hits += 1
            if profile is None:
                missing += 1
                continue
            profiles[asin] = profile

        return EnrichmentResult(
            profiles=profiles,
            stats=EnrichmentStats(
                requested_asins=len(asins),
                unique_asins=len(unique),
                primary_hits=primary_hits,
                fallback_hits=fallback_hits,
                missing_profiles=missing,
            ),
        )


def _is_complete_enough(profile: ASINProfile) -> bool:
    """Fields needed by classification and downstream market analysis.

    seller_region counts: a profile without a known region keeps going to fallbacks.
    """
    return bool(
        profile.title
        and profile.brand
        and profile.bullets
        and profile.seller_region not in {"", "UNKNOWN"}
    )
