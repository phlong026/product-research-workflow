from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class NewProductSnapshot:
    asin: str
    month: str
    monthly_sales: int
    launch_date: str | None = None
    seller_region: str = "UNKNOWN"
    account_type: str = "UNKNOWN"
    segment: str = "unknown"
    eligible: bool = True


@dataclass(slots=True, frozen=True)
class NewProductConfig:
    sales_threshold: int = 250
    consecutive_months: int = 3
    early_threshold_window_months: int = 3
    current_survival_threshold: int | None = None

    def validate(self) -> None:
        if self.sales_threshold < 0:
            raise ValueError("sales_threshold must be >= 0")
        if self.consecutive_months < 1:
            raise ValueError("consecutive_months must be >= 1")
        if self.early_threshold_window_months < 1:
            raise ValueError("early_threshold_window_months must be >= 1")
        if self.current_survival_threshold is not None and self.current_survival_threshold < 0:
            raise ValueError("current_survival_threshold must be >= 0")


@dataclass(slots=True, frozen=True)
class NewProductOutcome:
    asin: str
    anchor_month: str
    first_observed_month: str
    first_threshold_month: str | None
    first_threshold_month_index: int | None
    launch_success: bool
    success_month: str | None
    current_month: str
    current_sales: int | None
    current_observed: bool
    current_survival: bool | None
    seller_region: str
    account_type: str
    segment: str


@dataclass(slots=True, frozen=True)
class AccountTypeMetric:
    account_type: str
    total_products: int
    successful_products: int
    success_rate_pct: float
    share_of_successful_pct: float


@dataclass(slots=True, frozen=True)
class NewProductGroupMetric:
    dimension: str
    value: str
    total_products: int
    successful_products: int
    success_rate_pct: float
    share_of_successful_pct: float


@dataclass(slots=True, frozen=True)
class NewProductSummary:
    total_products: int
    successful_products: int
    success_rate_pct: float
    surviving_successful_products: int
    survival_rate_among_successful_pct: float | None
    successful_with_early_threshold: int
    early_threshold_share_among_successful_pct: float
    current_month: str | None
    observed_successful_products: int = 0
    unknown_successful_products: int = 0
    current_observation_coverage_pct: float | None = None


@dataclass(slots=True)
class NewProductAnalysisResult:
    outcomes: list[NewProductOutcome]
    summary: NewProductSummary
    account_types: list[AccountTypeMetric]
    seller_regions: list[NewProductGroupMetric]
    segments: list[NewProductGroupMetric]
    config: NewProductConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcomes": [asdict(row) for row in self.outcomes],
            "summary": asdict(self.summary),
            "account_types": [asdict(row) for row in self.account_types],
            "seller_regions": [asdict(row) for row in self.seller_regions],
            "segments": [asdict(row) for row in self.segments],
            "config": asdict(self.config),
        }
