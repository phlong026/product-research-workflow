from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityRoute:
    capability: str
    primary_provider: str
    fallback_providers: tuple[str, ...] = ()
    notes: str = ""


DEFAULT_CAPABILITY_ROUTES: tuple[CapabilityRoute, ...] = (
    CapabilityRoute("amazon_product_research", "sellersprite", ("sif",), "产品/类目/ASIN基础研究"),
    CapabilityRoute("amazon_market_concentration", "sellersprite", ("sif",), "商品/品牌/卖家集中度"),
    CapabilityRoute("amazon_reviews", "sellersprite", ("apify",), "评论数据"),
    CapabilityRoute("keyword_research", "sellersprite", ("sif", "xiyou"), "关键词与ABA"),
    CapabilityRoute("traffic_and_ads", "xiyou", ("sif",), "流量/广告趋势"),
    CapabilityRoute("public_web_research", "apify", (), "Google/站外公开网页等兜底"),
)


def route_for(capability: str) -> CapabilityRoute:
    for route in DEFAULT_CAPABILITY_ROUTES:
        if route.capability == capability:
            return route
    raise KeyError(f"Unknown research capability: {capability}")
