from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class RouteSuggestion:
    route_id: str
    title: str
    reason: str
    evidence_ids: tuple[str, ...]
    next_modules: tuple[str, ...]
    priority: str = "normal"


@dataclass(slots=True)
class ResearchRoutePlan:
    suggestions: list[RouteSuggestion]
    confirmed_route_ids: list[str] = field(default_factory=list)
    human_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestions": [asdict(item) for item in self.suggestions],
            "confirmed_route_ids": list(self.confirmed_route_ids),
            "human_confirmed": self.human_confirmed,
        }


@dataclass(slots=True, frozen=True)
class RouterConfig:
    small_market_sales_max: int | None = None
    fast_growth_pct: float | None = None
    material_share_change_pp: float | None = None

    def validate(self) -> None:
        if self.small_market_sales_max is not None and self.small_market_sales_max < 0:
            raise ValueError("small_market_sales_max must be >= 0")
        if self.fast_growth_pct is not None and self.fast_growth_pct < 0:
            raise ValueError("fast_growth_pct must be >= 0")
        if self.material_share_change_pp is not None and self.material_share_change_pp < 0:
            raise ValueError("material_share_change_pp must be >= 0")
