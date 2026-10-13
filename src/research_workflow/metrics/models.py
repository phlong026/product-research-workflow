from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    """Monthly seller-region structure.

    Denominator of ``sales_share_pct`` / ``asin_share_pct`` is the month total
    INCLUDING the ``UNKNOWN`` region (missing ``seller_region``).

    MoM/YoY fields are only filled when the base month exists in the data.
    A missing base month never yields a trend conclusion; it yields
    ``coverage_warning`` instead (``yoy_base_month_missing:YYYY-MM`` /
    ``mom_base_month_missing:YYYY-MM``), so seasonal swings and coverage
    changes can be told apart. ``cause_hypothesis`` is only ever a
    "待验证假设" label, never a verified cause.
    """

    month: str
    seller_region: str
    sales: int
    asin_count: int
    sales_share_pct: float
    asin_share_pct: float
    sales_mom_pct: float | None = None
    sales_yoy_pct: float | None = None
    share_mom_pp: float | None = None
    share_yoy_pp: float | None = None
    coverage_warning: str | None = None
    cause_hypothesis: str | None = None


@dataclass(slots=True, frozen=True)
class RegionSegmentMonthlyMetric:
    """Monthly seller-region x segment cross-tab cell (dense: zero cells kept).

    Denominators:
    - ``sales_share_of_month_pct``: month total sales INCLUDING ``UNKNOWN`` region.
    - ``sales_share_of_known_region_pct``: month sales EXCLUDING ``UNKNOWN`` region;
      ``None`` for the ``UNKNOWN`` region cells themselves.
    - ``share_within_region_pct``: sales of the (month, region) column.
    - ``asin_share_of_month_pct``: distinct ASINs of the month INCLUDING ``UNKNOWN``.
    ``asin_count`` is the distinct ASIN count of the cell.
    """

    month: str
    seller_region: str
    segment: str
    sales: int
    asin_count: int
    sales_share_of_month_pct: float
    sales_share_of_known_region_pct: float | None
    share_within_region_pct: float
    asin_share_of_month_pct: float


@dataclass(slots=True, frozen=True)
class SeasonalityYear:
    """High/low month of one calendar year.

    High/low are only reported for complete years (all 12 months present).
    Incomplete years carry ``coverage_warning`` and no high/low conclusion.
    """

    year: int
    months_observed: int
    missing_months: list[str]
    is_complete: bool
    high_month: str | None = None
    high_sales: int | None = None
    low_month: str | None = None
    low_sales: int | None = None
    coverage_warning: str | None = None
    cause_hypothesis: str | None = None


@dataclass(slots=True, frozen=True)
class SeasonalityResult:
    years: list[SeasonalityYear]
    complete_years: list[int]
    # Month-of-year ("01".."12") that is the high/low month of EVERY complete
    # year; requires >= 2 complete years, otherwise None.
    recurring_high_month: str | None = None
    recurring_low_month: str | None = None
    coverage_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    # Non-Optional on purpose: scripts/run_research.py `rebuild()` only coerces
    # plain list[dataclass] / dataclass hints, not `X | None` unions.
    region_segments: list[RegionSegmentMonthlyMetric] = field(default_factory=list)
    seasonality: SeasonalityResult = field(
        default_factory=lambda: SeasonalityResult(years=[], complete_years=[])
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": [asdict(row) for row in self.market],
            "segments": [asdict(row) for row in self.segments],
            "seller_regions": [asdict(row) for row in self.seller_regions],
            "decompositions": [asdict(row) for row in self.decompositions],
            "period_comparisons": [asdict(row) for row in (self.period_comparisons or [])],
            "region_segments": [asdict(row) for row in self.region_segments],
            "seasonality": self.seasonality.to_dict(),
        }
