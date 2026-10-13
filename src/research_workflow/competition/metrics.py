from __future__ import annotations

from collections import defaultdict
import unicodedata
from typing import Iterable

from research_workflow.data.models import BSRRecord

from .rank_matrix import DEFAULT_POINT_RANKS, build_point_rank_matrix, compare_point_rank_matrices
from .models import (
    CompetitionMetricsResult,
    ConcentrationMetric,
    HeadSuctionSignal,
    RankLayerMetric,
    RankBandMetric,
)

DEFAULT_RANK_CUTOFFS = (1, 5, 10, 20, 30, 50, 70, 100)
DEFAULT_RANK_BANDS = ((1, 5, "head_1_5"), (6, 30, "waist_6_30"), (31, 100, "tail_31_100"))


def _pct(part: float, total: float) -> float:
    return 0.0 if total == 0 else part / total * 100.0


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current / previous - 1.0) * 100.0


def _prior_year(month: str) -> str:
    year, mon = month[:7].split("-")
    return f"{int(year) - 1:04d}-{int(mon):02d}"


def _month(value: str) -> str:
    year, mon = value[:7].split("-")
    return f"{int(year):04d}-{int(mon):02d}"


def build_competition_metrics(
    records: Iterable[BSRRecord],
    *,
    rank_cutoffs: tuple[int, ...] = DEFAULT_RANK_CUTOFFS,
    head_cutoff: int = 10,
    broad_cutoff: int = 100,
    rank_bands: tuple[tuple[int, int, str], ...] = DEFAULT_RANK_BANDS,
    point_ranks: tuple[int, ...] = DEFAULT_POINT_RANKS,
) -> CompetitionMetricsResult:
    grouped: dict[str, list[BSRRecord]] = defaultdict(list)
    for record in records:
        grouped[_month(record.month)].append(record)

    concentration: list[ConcentrationMetric] = []
    rank_layers: list[RankLayerMetric] = []

    layer_sales: dict[tuple[str, int], int] = {}
    months = sorted(grouped)
    for month in months:
        rows = grouped[month]
        sales_sorted = sorted(rows, key=lambda r: (-max(0, r.monthly_sales), r.asin))
        total_sales = sum(max(0, r.monthly_sales) for r in rows)

        brand_sales: dict[str, int] = defaultdict(int)
        for row in rows:
            # Missing brand must not collapse unrelated ASINs into one fake brand.
            brand_key = unicodedata.normalize("NFKC", row.brand).strip().casefold() or f"<unknown:{row.asin}>"
            brand_sales[brand_key] += max(0, row.monthly_sales)
        brand_sorted = sorted(brand_sales.values(), reverse=True)

        seller_sales: dict[str, int] = defaultdict(int)
        unknown_seller_asins: set[str] = set()
        unknown_seller_sales = 0
        for row in rows:
            seller_id = row.seller_id.strip()
            sales = max(0, row.monthly_sales)
            if seller_id:
                seller_sales[f"seller:{seller_id}"] += sales
            else:
                # Missing seller must not collapse unrelated ASINs into one fake seller.
                seller_sales[f"<unknown_seller:{row.asin}>"] += sales
                unknown_seller_asins.add(row.asin)
                unknown_seller_sales += sales
        seller_sorted = sorted(seller_sales.values(), reverse=True)

        concentration.append(
            ConcentrationMetric(
                month=month,
                total_sales=total_sales,
                asin_count=len({r.asin for r in rows}),
                top3_asin_share_pct=_pct(
                    sum(max(0, r.monthly_sales) for r in sales_sorted[:3]), total_sales
                ),
                top10_asin_share_pct=_pct(
                    sum(max(0, r.monthly_sales) for r in sales_sorted[:10]), total_sales
                ),
                top3_brand_share_pct=_pct(sum(brand_sorted[:3]), total_sales),
                top10_brand_share_pct=_pct(sum(brand_sorted[:10]), total_sales),
                top3_seller_share_pct=_pct(sum(seller_sorted[:3]), total_sales),
                top10_seller_share_pct=_pct(sum(seller_sorted[:10]), total_sales),
                unknown_seller_sales=unknown_seller_sales,
                unknown_seller_asin_count=len(unknown_seller_asins),
                unknown_seller_share_pct=_pct(unknown_seller_sales, total_sales),
            )
        )

        # Rank layer = cumulative products ordered by BSR/category rank. If rank is
        # missing, fallback rows are appended by sales only after all ranked rows.
        ranked = sorted(
            rows,
            key=lambda r: (
                r.node_rank is None,
                r.node_rank if r.node_rank is not None else 10**9,
                -max(0, r.monthly_sales),
                r.asin,
            ),
        )
        for cutoff in sorted(set(rank_cutoffs)):
            selected = ranked[:cutoff]
            sales = sum(max(0, r.monthly_sales) for r in selected)
            layer_sales[(month, cutoff)] = sales

    for month in months:
        total_sales = sum(max(0, r.monthly_sales) for r in grouped[month])
        ranked_count = len(grouped[month])
        for cutoff in sorted(set(rank_cutoffs)):
            sales = layer_sales[(month, cutoff)]
            prior = _prior_year(month)
            prior_sales = layer_sales.get((prior, cutoff))
            rank_layers.append(
                RankLayerMetric(
                    month=month,
                    cutoff=cutoff,
                    sales=sales,
                    asin_count=min(cutoff, ranked_count),
                    sales_share_pct=_pct(sales, total_sales),
                    yoy_pct=(
                        _pct_change(sales, prior_sales)
                        if prior_sales is not None
                        else None
                    ),
                )
            )

    rank_band_metrics: list[RankBandMetric] = []
    band_sales: dict[tuple[str, str], int] = {}
    for month in months:
        total_sales = sum(max(0, r.monthly_sales) for r in grouped[month])
        ranked = sorted(
            grouped[month],
            key=lambda r: (r.node_rank is None, r.node_rank if r.node_rank is not None else 10**9, -max(0, r.monthly_sales), r.asin),
        )
        for start_rank, end_rank, label in rank_bands:
            if start_rank < 1 or end_rank < start_rank:
                raise ValueError("invalid rank band")
            selected = ranked[start_rank - 1 : end_rank]
            sales = sum(max(0, r.monthly_sales) for r in selected)
            band_sales[(month, label)] = sales
            prior_sales = band_sales.get((_prior_year(month), label))
            rank_band_metrics.append(RankBandMetric(
                month=month, band=label, start_rank=start_rank, end_rank=end_rank,
                sales=sales, asin_count=len(selected), sales_share_pct=_pct(sales, total_sales),
                yoy_pct=_pct_change(sales, prior_sales) if prior_sales is not None else None,
            ))

    head_suction: list[HeadSuctionSignal] = []
    for month in months:
        prior = _prior_year(month)
        head_current = layer_sales.get((month, head_cutoff))
        head_prior = layer_sales.get((prior, head_cutoff))
        broad_current = layer_sales.get((month, broad_cutoff))
        broad_prior = layer_sales.get((prior, broad_cutoff))
        if None in {head_current, head_prior, broad_current, broad_prior}:
            continue
        head_yoy = _pct_change(float(head_current), float(head_prior))
        broad_yoy = _pct_change(float(broad_current), float(broad_prior))
        gap = None if head_yoy is None or broad_yoy is None else head_yoy - broad_yoy
        if head_yoy is not None and broad_yoy is not None and head_yoy > 0 > broad_yoy:
            pattern = "head_up_broad_down"
        elif head_yoy is not None and broad_yoy is not None and head_yoy > broad_yoy:
            pattern = "head_outperforming"
        elif head_yoy is not None and broad_yoy is not None and head_yoy < broad_yoy:
            pattern = "head_underperforming"
        else:
            pattern = "flat_or_unresolved"
        head_suction.append(
            HeadSuctionSignal(
                month=month,
                base_month=prior,
                head_cutoff=head_cutoff,
                broad_cutoff=broad_cutoff,
                head_yoy_pct=head_yoy,
                broad_yoy_pct=broad_yoy,
                head_minus_broad_pp=gap,
                pattern=pattern,
            )
        )

    # Single-rank (point) matrix lives next to the cumulative layers above; the two
    # bases are never mixed. Siphon-style comparison must use the point changes.
    point_matrix = build_point_rank_matrix(
        (record for rows in grouped.values() for record in rows), ranks=point_ranks
    )

    return CompetitionMetricsResult(
        concentration=concentration,
        rank_layers=rank_layers,
        rank_bands=rank_band_metrics,
        head_suction=head_suction,
        point_rank_matrix=point_matrix,
        point_rank_changes=compare_point_rank_matrices(point_matrix),
    )
