from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class MonthlyMarketMetric:
    month: str
    total_sales: int
    asin_count: int
    mom_pct: float | None = None
    yoy_pct: float | None = None


@dataclass(slots=True, frozen=True)
class SegmentMonthlyMetric:
    month: str
    segment: str
    sales: int
    asin_count: int
    sales_share_pct: float
    asin_share_pct: float
    mom_pct: float | None = None
    yoy_pct: float | None = None


@dataclass(slots=True, frozen=True)
class SellerRegionMonthlyMetric:
    month: str
    seller_region: str
    sales: int
    asin_count: int
    sales_share_pct: float
    asin_share_pct: float


@dataclass(slots=True, frozen=True)
class SegmentChangeDecomposition:
    segment: str
    base_month: str
    current_month: str
    base_market_sales: int
    current_market_sales: int
    base_segment_sales: int
    current_segment_sales: int
    market_change_pct: float | None
    segment_change_pct: float | None
    share_change_pp: float
    total_change_sales: float
    market_contribution_sales: float
    share_contribution_sales: float

    @property
    def reconstructed_change_sales(self) -> float:
        return self.market_contribution_sales + self.share_contribution_sales


@dataclass(slots=True, frozen=True)
class PeriodComparisonMetric:
    scope: str
    segment: str | None
    base_period: str
    current_period: str
    base_sales: int | None
    current_sales: int | None
    change_pct: float | None
    base_share_pct: float | None = None
    current_share_pct: float | None = None
    share_change_pp: float | None = None
    missing_base_months: tuple[str, ...] = ()
    missing_current_months: tuple[str, ...] = ()


@dataclass(slots=True)
class MarketMetricsResult:
    market: list[MonthlyMarketMetric]
    segments: list[SegmentMonthlyMetric]
    seller_regions: list[SellerRegionMonthlyMetric]
    decompositions: list[SegmentChangeDecomposition]
    period_comparisons: list[PeriodComparisonMetric] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": [asdict(row) for row in self.market],
            "segments": [asdict(row) for row in self.segments],
            "seller_regions": [asdict(row) for row in self.seller_regions],
            "decompositions": [asdict(row) for row in self.decompositions],
            "period_comparisons": [asdict(row) for row in (self.period_comparisons or [])],
        }
