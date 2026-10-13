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
    parent_asin: str = ""


@dataclass(slots=True, frozen=True)
class NewProductConfig:
    """New-product analysis configuration.

    Month boundary: everything is counted in natural calendar months (YYYY-MM).
    The month containing the launch date is month 1 (a launch on the 31st still
    counts the whole month); without a launch date the first observed month is
    month 1.

    Two independent thresholds (NOTE: default values need manual confirmation):
    - ``promotion_waist_sales_threshold``: the manual "waist" monthly sales used
      to decide launch success (promotion). ``None`` falls back to the legacy
      ``sales_threshold`` (default 250).
    - ``retention_sales_threshold``: monthly sales used for the retention rate
      and the current-survival check. ``None`` falls back to the legacy
      ``current_survival_threshold`` and then ``sales_threshold`` (default 250).

    ``study_month`` (optional, YYYY-MM): when set, the latest month present in
    the input must equal it, otherwise ``ValueError`` is raised.
    """

    sales_threshold: int = 250
    consecutive_months: int = 3
    early_threshold_window_months: int = 3
    current_survival_threshold: int | None = None
    promotion_waist_sales_threshold: int | None = None
    retention_sales_threshold: int | None = None
    study_month: str | None = None

    @property
    def effective_promotion_threshold(self) -> int:
        if self.promotion_waist_sales_threshold is not None:
            return self.promotion_waist_sales_threshold
        return self.sales_threshold

    @property
    def effective_retention_threshold(self) -> int:
        if self.retention_sales_threshold is not None:
            return self.retention_sales_threshold
        if self.current_survival_threshold is not None:
            return self.current_survival_threshold
        return self.sales_threshold

    def validate(self) -> None:
        if self.promotion_waist_sales_threshold is not None and self.promotion_waist_sales_threshold < 0:
            raise ValueError("promotion_waist_sales_threshold must be >= 0")
        if self.retention_sales_threshold is not None and self.retention_sales_threshold < 0:
            raise ValueError("retention_sales_threshold must be >= 0")
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
    # --- appended fields (backward compatible defaults) ---
    early_window_complete: bool = False
    early_window_reached: bool = False
    # "retained" / "not_retained" / "unknown" (unknown = current month not observed
    # while the historical consecutive-run condition is already met)
    retention_status: str = "not_retained"
    # "yes" / "no" / "unknown":中间缺月 = 未知(不当 0)。unknown = 已观测月份无法凑齐
    # 连续达标段,但缺月若达标则可补足;launch_success 布尔字段在 unknown 时为 False(兼容旧口径)。
    launch_success_status: str = "no"


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
    # --- appended fields (backward compatible defaults) ---
    promotion_waist_sales_threshold: int = 250
    retention_sales_threshold: int = 250
    # Retention: numerator = "N consecutive months >= retention threshold AND current
    # month still >= retention threshold"; denominator = ALL valid new products.
    # Unknown (current month unobserved, history already qualifies) is NOT counted as
    # retained in retention_rate_pct (lower bound) and is counted in the upper bound.
    retention_denominator_products: int = 0
    retained_products: int = 0
    retention_unknown_products: int = 0
    retention_rate_pct: float = 0.0
    retention_rate_upper_bound_pct: float = 0.0
    retention_denominator_note: str = ""
    # Launch success 三态:successful_products 为下限(仅 yes),unknown 为缺月无法判定的新品数,
    # success_rate_upper_bound_pct = (yes + unknown) / total。
    launch_success_unknown_products: int = 0
    success_rate_upper_bound_pct: float = 0.0
    # Early window: denominator = valid new products whose whole early window is observed.
    early_window_complete_products: int = 0
    early_window_incomplete_products: int = 0
    early_window_reached_products: int = 0
    early_window_reached_share_pct: float = 0.0


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
