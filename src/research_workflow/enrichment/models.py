from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ASINProfile:
    """Vendor-neutral product profile used by downstream research modules."""

    asin: str
    title: str = ""
    bullets: list[str] = field(default_factory=list)
    brand: str = ""
    parent_asin: str = ""
    launch_date: str | None = None
    seller_region: str = "UNKNOWN"
    image_url: str | None = None
    price: float | None = None
    rating: float | None = None
    review_count: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class EnrichmentStats:
    requested_asins: int
    unique_asins: int
    primary_hits: int
    fallback_hits: int
    missing_profiles: int


@dataclass(slots=True)
class EnrichmentResult:
    profiles: dict[str, ASINProfile]
    stats: EnrichmentStats
