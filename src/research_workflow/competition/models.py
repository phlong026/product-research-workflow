from __future__ import annotations

from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "concentration": [asdict(row) for row in self.concentration],
            "rank_layers": [asdict(row) for row in self.rank_layers],
            "rank_bands": [asdict(row) for row in self.rank_bands],
            "head_suction": [asdict(row) for row in self.head_suction],
        }
