from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from research_workflow.data.bsr_pipeline import normalize_seller_region

from .models import ASINProfile


ALIASES: dict[str, tuple[str, ...]] = {
    "asin": ("asin", "ASIN"),
    "title": ("title", "productTitle", "商品标题", "标题"),
    "brand": ("brand", "brandName", "品牌"),
    "parent_asin": ("parentAsin", "parent_asin", "parentASIN", "父ASIN"),
    "launch_date": ("launchDate", "launch_date", "listingDate", "firstAvailable", "上架时间", "上架日期"),
    "seller_region": ("sellerRegion", "seller_region", "sellerCountry", "sellerLocation", "卖家所属地", "卖家所在地"),
    "image_url": ("image", "imageUrl", "image_url", "mainImage", "商品主图"),
    "price": ("price", "currentPrice", "价格"),
    "rating": ("rating", "stars", "评分"),
    "review_count": ("ratings", "reviewCount", "review_count", "reviews", "评论数"),
}

BULLET_ALIASES = (
    "bullets",
    "bulletPoints",
    "bullet_points",
    "features",
    "sellingPoints",
    "卖点",
    "五点描述",
)


# Amazon account types ("VC" = Vendor Central) are not geography. They must never
# land in seller_region; they are kept separately as attributes["account_type"].
_ACCOUNT_TYPE_TOKENS: dict[str, str] = {
    "VC": "VC",
    "VENDOR": "VC",
    "VENDORCENTRAL": "VC",
    "SC": "SC",
    "SELLERCENTRAL": "SC",
}


def split_region_and_account_type(value: Any) -> tuple[str, str | None]:
    """Return (normalized geographic region, account type or None)."""
    token = str(value or "").strip().upper().replace(" ", "")
    account_type = _ACCOUNT_TYPE_TOKENS.get(token)
    if account_type:
        return "UNKNOWN", account_type
    return normalize_seller_region(value), None


def _first(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    for key in aliases:
        value = mapping.get(key)
        if value is not None and value != "" and value != []:
            return value
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        return float(text)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _normalize_bullets(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # Some providers return newline-joined bullet text.
        if "\n" in text:
            return [item.strip(" •\t") for item in text.splitlines() if item.strip(" •\t")]
        return [text]
    if isinstance(value, Mapping):
        return [str(v).strip() for v in value.values() if str(v).strip()]
    if isinstance(value, Iterable):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _unwrap_payload(payload: Any, asin: str | None = None) -> Mapping[str, Any] | None:
    """Find the most likely product-detail mapping in common MCP/API envelopes."""
    if not isinstance(payload, Mapping):
        return None

    # Direct object first.
    if _first(payload, ALIASES["asin"]) or _first(payload, ALIASES["title"]):
        return payload

    for key in ("data", "result", "item", "product", "detail", "payload"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            candidate = _unwrap_payload(nested, asin)
            if candidate:
                return candidate
        elif isinstance(nested, list):
            candidate = _find_in_list(nested, asin)
            if candidate:
                return candidate

    # Some providers return ASIN keyed dictionaries.
    if asin:
        for key in (asin, asin.upper(), asin.lower()):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return nested
    return None


def _find_in_list(items: list[Any], asin: str | None) -> Mapping[str, Any] | None:
    candidates = [item for item in items if isinstance(item, Mapping)]
    if not candidates:
        return None
    if asin:
        wanted = asin.upper()
        for item in candidates:
            value = _first(item, ALIASES["asin"])
            if value and str(value).upper() == wanted:
                return item
        return None
    return candidates[0]


def profile_from_payload(payload: Any, *, asin: str, source: str) -> ASINProfile | None:
    item = _unwrap_payload(payload, asin)
    if not item:
        return None

    actual_asin = str(_first(item, ALIASES["asin"]) or asin).strip().upper()
    if actual_asin != asin.strip().upper():
        return None
    title = str(_first(item, ALIASES["title"]) or "").strip()
    bullets = _normalize_bullets(_first(item, BULLET_ALIASES))
    seller_region, account_type = split_region_and_account_type(
        _first(item, ALIASES["seller_region"])
    )

    known = {name for aliases in ALIASES.values() for name in aliases} | set(BULLET_ALIASES)
    attributes = {k: v for k, v in item.items() if k not in known}
    if account_type:
        attributes.setdefault("account_type", account_type)

    return ASINProfile(
        asin=actual_asin,
        title=title,
        bullets=bullets,
        brand=str(_first(item, ALIASES["brand"]) or "").strip(),
        parent_asin=str(_first(item, ALIASES["parent_asin"]) or "").strip().upper(),
        launch_date=str(_first(item, ALIASES["launch_date"]) or "").strip() or None,
        seller_region=seller_region,
        image_url=str(_first(item, ALIASES["image_url"]) or "").strip() or None,
        price=_as_float(_first(item, ALIASES["price"])),
        rating=_as_float(_first(item, ALIASES["rating"])),
        review_count=_as_int(_first(item, ALIASES["review_count"])),
        attributes=attributes,
        sources=[source],
    )


def merge_profiles(primary: ASINProfile, fallback: ASINProfile) -> ASINProfile:
    """Fill only missing fields. Primary provider always wins on populated values."""
    if primary.asin != fallback.asin:
        raise ValueError("Cannot merge profiles for different ASINs")

    return ASINProfile(
        asin=primary.asin,
        title=primary.title or fallback.title,
        bullets=primary.bullets or fallback.bullets,
        brand=primary.brand or fallback.brand,
        parent_asin=primary.parent_asin or fallback.parent_asin,
        launch_date=primary.launch_date or fallback.launch_date,
        seller_region=(
            primary.seller_region
            if primary.seller_region not in {"", "UNKNOWN"}
            else fallback.seller_region
        ),
        image_url=primary.image_url or fallback.image_url,
        price=primary.price if primary.price is not None else fallback.price,
        rating=primary.rating if primary.rating is not None else fallback.rating,
        review_count=(
            primary.review_count if primary.review_count is not None else fallback.review_count
        ),
        attributes={**fallback.attributes, **primary.attributes},
        sources=list(dict.fromkeys(primary.sources + fallback.sources)),
    )
