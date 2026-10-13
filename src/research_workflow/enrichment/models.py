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


@dataclass(slots=True, frozen=True)
class EnrichmentConflict:
    """Same field, different values across sources. Never resolved silently."""

    asin: str
    field: str
    values: dict[str, str]  # source name -> value
    chosen: str  # value kept (first source in provider order); needs human review
    month: str | None = None


@dataclass(slots=True, frozen=True)
class EnrichmentFailure:
    asin: str
    source: str
    error: str


@dataclass(slots=True)
class EnrichmentReport:
    """Result of a targeted backfill of seller_region."""

    profiles: dict[str, ASINProfile]
    total_profiles: int
    targeted: int
    filled: int
    unknown_before: int
    unknown_after: int
    conflicts: list[EnrichmentConflict] = field(default_factory=list)
    failures: list[EnrichmentFailure] = field(default_factory=list)

    @property
    def unknown_ratio_before(self) -> float:
        return self.unknown_before / self.total_profiles if self.total_profiles else 0.0

    @property
    def unknown_ratio_after(self) -> float:
        return self.unknown_after / self.total_profiles if self.total_profiles else 0.0


@dataclass(slots=True)
class BulletBackfill:
    """Result of backfilling bullets once per unique ASIN across all months."""

    bullets_by_asin: dict[str, list[str]]
    filled_records: dict[str, list[dict[str, Any]]]
    unique_asins: int
    fetched_asins: int
    missing_asins: list[str] = field(default_factory=list)
    conflicts: list[EnrichmentConflict] = field(default_factory=list)
    failures: list[EnrichmentFailure] = field(default_factory=list)
