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
    # Seller basis (seller_id). Records without seller_id are NOT merged into one
    # seller: each is its own bucket when ranking Top-N, and they are reported
    # separately in the unknown_seller_* fields (share denominator = total_sales).
    top3_seller_share_pct: float = 0.0
    top10_seller_share_pct: float = 0.0
    unknown_seller_sales: int = 0
    unknown_seller_asin_count: int = 0
    unknown_seller_share_pct: float = 0.0


@dataclass(slots=True, frozen=True)
class RankLayerMetric:
    month: str
    cutoff: int
    sales: int
    asin_count: int
    sales_share_pct: float
    yoy_pct: float | None = None
    # Cumulative TopN (ranks 1..cutoff summed), ordered by node_rank. Not a single-rank value.
    basis: str = "cumulative"


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
    # Interval sum (head/waist/tail band), ordered by node_rank. Not a single-rank value.
    basis: str = "cumulative"


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
    # Compares two cumulative TopN layers; single-rank (point) changes live in point_rank_changes.
    basis: str = "cumulative"


@dataclass(slots=True)
class PointRankRow:
    """Single-rank sales of one month. ``point_sales["3"]`` is the sales of the 3rd
    ranked ASIN only (NOT cumulative); ``None`` = fewer ASINs than that rank.

    ``ordering_basis``: ``sales_rank`` (all records have it), ``monthly_sales_desc``
    (none has it) or ``mixed`` (ranked records first, the rest by sales desc).
    Keys are rank numbers as strings so the row survives a JSON round trip.
    """

    month: str
    ordering_basis: str
    point_sales: dict[str, int | None]
    point_asin: dict[str, str | None]
    basis: str = "point"


@dataclass(slots=True)
class PointRankMatrix:
    ranks: list[int] = field(default_factory=list)
    rows: list[PointRankRow] = field(default_factory=list)
    basis: str = "point"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class PointRankChange:
    """Factual change of one single-rank cell between two months (no advice).

    ``direction`` is one of up / down / flat / unavailable (either side missing).
    ``rel_change_pct`` is None when the base is 0 or unavailable.
    """

    base_month: str
    current_month: str
    rank: int
    base_point_sales: int | None
    current_point_sales: int | None
    abs_change: int | None
    rel_change_pct: float | None
    direction: str
    basis: str = "point"


@dataclass(slots=True)
class CompetitionMetricsResult:
    concentration: list[ConcentrationMetric]
    rank_layers: list[RankLayerMetric]
    rank_bands: list[RankBandMetric]
    head_suction: list[HeadSuctionSignal]
    point_rank_matrix: PointRankMatrix = field(default_factory=PointRankMatrix)
    point_rank_changes: list[PointRankChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "concentration": [asdict(row) for row in self.concentration],
            "rank_layers": [asdict(row) for row in self.rank_layers],
            "rank_bands": [asdict(row) for row in self.rank_bands],
            "head_suction": [asdict(row) for row in self.head_suction],
            "point_rank_matrix": self.point_rank_matrix.to_dict(),
            "point_rank_changes": [asdict(row) for row in self.point_rank_changes],
        }
