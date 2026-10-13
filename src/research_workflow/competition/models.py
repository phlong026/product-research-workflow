from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class ConcentrationMetric:
    month: str
    total_sales: int
    asin_count: int
    top3_asin_share_pct: float
    top10_asin_share_pct: float
    top3_brand_share_pct: float
    top10_brand_share_pct: float
    # 卖家集中度必须由真实卖家名计算，不得用品牌顶替；无卖家数据时为 None（未知）。
    top3_seller_share_pct: float | None = None
    top10_seller_share_pct: float | None = None
    # 覆盖率 <100% 时，缺失者各自独立成组，占比为下界。
    seller_coverage_pct: float = 0.0
    brand_coverage_pct: float = 0.0


@dataclass(slots=True, frozen=True)
class RankPointMetric:
    """单个名次自身的销量（非累计）。sales 为 None 表示该名次无数据，不是零。"""

    month: str
    rank: int
    asin: str | None
    sales: int | None
    sales_share_pct: float | None
    yoy_pct: float | None = None
    rank_basis: str = "node_rank"


@dataclass(slots=True, frozen=True)
class RankLayerMetric:
    month: str
    cutoff: int
    sales: int
    asin_count: int
    sales_share_pct: float
    yoy_pct: float | None = None


@dataclass(slots=True, frozen=True)
class RankBandMetric:
    month: str
    band: str
    start_rank: int
    end_rank: int
    sales: int
    asin_count: int
    sales_share_pct: float
    yoy_pct: float | None = None


@dataclass(slots=True, frozen=True)
class HeadSuctionSignal:
    month: str
    base_month: str
    head_cutoff: int
    broad_cutoff: int
    head_yoy_pct: float | None
    broad_yoy_pct: float | None
    head_minus_broad_pp: float | None
    pattern: str


@dataclass(slots=True)
class CompetitionMetricsResult:
    concentration: list[ConcentrationMetric]
    rank_layers: list[RankLayerMetric]
    rank_bands: list[RankBandMetric]
    head_suction: list[HeadSuctionSignal]
    rank_points: list[RankPointMetric] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "concentration": [asdict(row) for row in self.concentration],
            "rank_layers": [asdict(row) for row in self.rank_layers],
            "rank_bands": [asdict(row) for row in self.rank_bands],
            "head_suction": [asdict(row) for row in self.head_suction],
            "rank_points": [asdict(row) for row in self.rank_points],
        }
