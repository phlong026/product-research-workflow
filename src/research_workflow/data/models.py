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
    seller: str = ""
    rating: float | None = None
    rating_count: int | None = None
    node_ids: list[str] = field(default_factory=list)
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
    metric_deduped_rows: int | None = None


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
    duplicate_metric_rows_removed: int = 0
    # 缺评分/评分数的行无法按“销量+评分+评分数相同”判重，保留并计数，不当作重复也不当作零。
    dedupe_unjudgeable_rows: int = 0
    missing_seller_rows: int = 0

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


@dataclass(slots=True)
class MonthlyBSRResult:
    month: str
    records: list[BSRRecord]
    stats: PipelineStats
    quality: DataQualityReport | None = None
    # 每一组被合并的重复商品：保留谁、删掉谁、依据什么，供人工抽查。
    dedupe_audit: list[dict[str, Any]] | None = None
