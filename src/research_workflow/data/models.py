from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class BSRRecord:
    asin: str
    month: str
    monthly_sales: int
    brand: str = ""
    title: str = ""
    parent_asin: str = ""
    seller_region: str = ""
    node_rank: int | None = None
    launch_date: str | None = None
    image_url: str | None = None
    seller_id: str = ""
    rating: float | None = None
    rating_count: int | None = None
    sales_rank: int | None = None  # 清洗后按销量降序重排的 1..N 名次;node_rank 保留源 BSR 名次
    source_row: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_row", None)
        return data


@dataclass(slots=True, frozen=True)
class PipelineStats:
    input_rows: int
    parsed_rows: int
    parent_deduped_rows: int
    suspicious_deduped_rows: int
    output_rows: int


@dataclass(slots=True, frozen=True)
class DataQualityReport:
    missing_asin_rows: int = 0
    invalid_sales_rows: int = 0
    missing_title_rows: int = 0
    missing_brand_rows: int = 0
    unknown_seller_region_rows: int = 0
    missing_rank_rows: int = 0
    duplicate_parent_rows_removed: int = 0
    duplicate_title_rows_removed: int = 0

    @property
    def warning_count(self) -> int:
        return (
            self.missing_asin_rows
            + self.invalid_sales_rows
            + self.missing_title_rows
            + self.missing_brand_rows
            + self.unknown_seller_region_rows
            + self.missing_rank_rows
        )


@dataclass(slots=True, frozen=True)
class DedupeAuditEntry:
    """一条去重审计:removed_asin 因 rule 被折叠进 kept_asin。"""

    month: str
    rule: str
    kept_asin: str
    removed_asin: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MonthlyBSRResult:
    month: str
    records: list[BSRRecord]
    stats: PipelineStats
    quality: DataQualityReport | None = None
    dedupe_audit: list[DedupeAuditEntry] = field(default_factory=list)
