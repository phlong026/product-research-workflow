from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, Mapping

from research_workflow.classification.models import ProductClassification
from research_workflow.data.models import BSRRecord

from .models import (
    MarketMetricsResult,
    MonthlyMarketMetric,
    SegmentChangeDecomposition,
    SegmentMonthlyMetric,
    SellerRegionMonthlyMetric,
    PeriodComparisonMetric,
)


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current / previous - 1.0) * 100.0


def _share(part: float, total: float) -> float:
    return 0.0 if total == 0 else part / total * 100.0


def _month_key(value: str) -> tuple[int, int]:
    text = value.strip()[:7]
    try:
        year, month = text.split("-")
        d = date(int(year), int(month), 1)
    except Exception as exc:
        raise ValueError(f"month must be YYYY-MM compatible, got {value!r}") from exc
    return d.year, d.month


def _previous_month(month: str) -> str:
    year, mon = _month_key(month)
    if mon == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{mon - 1:02d}"


def _previous_year(month: str) -> str:
    year, mon = _month_key(month)
    return f"{year - 1:04d}-{mon:02d}"


def _classification_label(value: str | ProductClassification | None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, ProductClassification):
        return value.label
    return str(value) or "unknown"


def build_market_metrics(
    records: Iterable[BSRRecord],
    classifications: Mapping[str, str | ProductClassification],
    *,
    decomposition_pairs: Iterable[tuple[str, str]] | None = None,
    period_comparison_pairs: Iterable[tuple[str, str, str, str]] | None = None,
) -> MarketMetricsResult:
    """Compute deterministic market metrics from approved monthly BSR snapshots.

    `records` is expected to be the cleaned monthly set produced by T03.
    `classifications` should come from an approved T05 classification version.
    """
    rows = list(records)
    market_sales: dict[str, int] = defaultdict(int)
    market_asins: dict[str, set[str]] = defaultdict(set)
    segment_sales: dict[tuple[str, str], int] = defaultdict(int)
    segment_asins: dict[tuple[str, str], set[str]] = defaultdict(set)
    region_sales: dict[tuple[str, str], int] = defaultdict(int)
    region_asins: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in rows:
        month = f"{_month_key(row.month)[0]:04d}-{_month_key(row.month)[1]:02d}"
        asin = row.asin.upper()
        segment = _classification_label(classifications.get(asin))
        region = row.seller_region or "UNKNOWN"
        sales = max(0, int(row.monthly_sales))

        market_sales[month] += sales
        market_asins[month].add(asin)
        segment_sales[(month, segment)] += sales
        segment_asins[(month, segment)].add(asin)
        region_sales[(month, region)] += sales
        region_asins[(month, region)].add(asin)

    months = sorted(market_sales, key=_month_key)
    market_metrics: list[MonthlyMarketMetric] = []
    for month in months:
        current = market_sales[month]
        market_metrics.append(
            MonthlyMarketMetric(
                month=month,
                total_sales=current,
                asin_count=len(market_asins[month]),
                mom_pct=(
                    _pct_change(current, market_sales[_previous_month(month)])
                    if _previous_month(month) in market_sales
                    else None
                ),
                yoy_pct=(
                    _pct_change(current, market_sales[_previous_year(month)])
                    if _previous_year(month) in market_sales
                    else None
                ),
            )
        )

    segment_metrics: list[SegmentMonthlyMetric] = []
    segments = sorted({segment for _, segment in segment_sales})
    for month in months:
        for segment in segments:
            key = (month, segment)
            sales = segment_sales.get(key, 0)
            count = len(segment_asins.get(key, set()))
            prev_month_key = (_previous_month(month), segment)
            prev_year_key = (_previous_year(month), segment)
            segment_metrics.append(
                SegmentMonthlyMetric(
                    month=month,
                    segment=segment,
                    sales=sales,
                    asin_count=count,
                    sales_share_pct=_share(sales, market_sales[month]),
                    asin_share_pct=_share(count, len(market_asins[month])),
                    mom_pct=(
                        _pct_change(sales, segment_sales[prev_month_key])
                        if prev_month_key in segment_sales
                        else None
                    ),
                    yoy_pct=(
                        _pct_change(sales, segment_sales[prev_year_key])
                        if prev_year_key in segment_sales
                        else None
                    ),
                )
            )

    region_metrics: list[SellerRegionMonthlyMetric] = []
    regions = sorted({region for _, region in region_sales})
    for month in months:
        for region in regions:
            key = (month, region)
            sales = region_sales.get(key, 0)
            count = len(region_asins.get(key, set()))
            region_metrics.append(
                SellerRegionMonthlyMetric(
                    month=month,
                    seller_region=region,
                    sales=sales,
                    asin_count=count,
                    sales_share_pct=_share(sales, market_sales[month]),
                    asin_share_pct=_share(count, len(market_asins[month])),
                )
            )

    pairs = list(decomposition_pairs or _default_decomposition_pairs(months))
    decompositions: list[SegmentChangeDecomposition] = []
    for base_month, current_month in pairs:
        if base_month not in market_sales or current_month not in market_sales:
            continue
        for segment in segments:
            base_market = market_sales[base_month]
            current_market = market_sales[current_month]
            base_segment = segment_sales.get((base_month, segment), 0)
            current_segment = segment_sales.get((current_month, segment), 0)
            base_share = 0.0 if base_market == 0 else base_segment / base_market
            current_share = 0.0 if current_market == 0 else current_segment / current_market

            # Exact two-factor decomposition:
            # M1*S1 - M0*S0 = (M1-M0)*S0 + M1*(S1-S0)
            market_contribution = (current_market - base_market) * base_share
            share_contribution = current_market * (current_share - base_share)
            total_change = float(current_segment - base_segment)

            decompositions.append(
                SegmentChangeDecomposition(
                    segment=segment,
                    base_month=base_month,
                    current_month=current_month,
                    base_market_sales=base_market,
                    current_market_sales=current_market,
                    base_segment_sales=base_segment,
                    current_segment_sales=current_segment,
                    market_change_pct=_pct_change(current_market, base_market),
                    segment_change_pct=_pct_change(current_segment, base_segment),
                    share_change_pp=(current_share - base_share) * 100.0,
                    total_change_sales=total_change,
                    market_contribution_sales=market_contribution,
                    share_contribution_sales=share_contribution,
                )
            )

    period_comparisons: list[PeriodComparisonMetric] = []
    for base_start, base_end, current_start, current_end in period_comparison_pairs or []:
        base_months = [m for m in months if _month_key(base_start) <= _month_key(m) <= _month_key(base_end)]
        current_months = [m for m in months if _month_key(current_start) <= _month_key(m) <= _month_key(current_end)]
        if not base_months or not current_months:
            continue
        base_market = sum(market_sales[m] for m in base_months)
        current_market = sum(market_sales[m] for m in current_months)
        base_label = f"{base_start[:7]}..{base_end[:7]}"
        current_label = f"{current_start[:7]}..{current_end[:7]}"
        period_comparisons.append(PeriodComparisonMetric(
            scope="market", segment=None, base_period=base_label, current_period=current_label,
            base_sales=base_market, current_sales=current_market, change_pct=_pct_change(current_market, base_market),
        ))
        for segment in segments:
            base_segment = sum(segment_sales.get((m, segment), 0) for m in base_months)
            current_segment = sum(segment_sales.get((m, segment), 0) for m in current_months)
            base_share = _share(base_segment, base_market)
            current_share = _share(current_segment, current_market)
            period_comparisons.append(PeriodComparisonMetric(
                scope="segment", segment=segment, base_period=base_label, current_period=current_label,
                base_sales=base_segment, current_sales=current_segment, change_pct=_pct_change(current_segment, base_segment),
                base_share_pct=base_share, current_share_pct=current_share, share_change_pp=current_share-base_share,
            ))

    return MarketMetricsResult(
        market=market_metrics,
        segments=segment_metrics,
        seller_regions=region_metrics,
        decompositions=decompositions,
        period_comparisons=period_comparisons,
    )


def _default_decomposition_pairs(months: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    available = set(months)
    for current in months:
        prior = _previous_year(current)
        if prior in available:
            pairs.append((prior, current))
    return pairs
