from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, Mapping

from research_workflow.classification.models import ProductClassification
from research_workflow.data.models import BSRRecord

from .models import (
    MarketMetricsResult,
    MonthlyMarketMetric,
    RegionSegmentMonthlyMetric,
    SegmentChangeDecomposition,
    SegmentMonthlyMetric,
    SellerRegionMonthlyMetric,
    PeriodComparisonMetric,
)
from .seasonality import detect_seasonality

UNKNOWN_REGION = "UNKNOWN"
CAUSE_HYPOTHESIS_REGION_COVERAGE = (
    "待验证假设:基期月份缺失,无法区分季节波动与覆盖变化;未经验证,不构成原因结论"
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
    cell_sales: dict[tuple[str, str, str], int] = defaultdict(int)
    cell_asins: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for row in rows:
        month = f"{_month_key(row.month)[0]:04d}-{_month_key(row.month)[1]:02d}"
        asin = row.asin.upper()
        segment = _classification_label(classifications.get(asin))
        region = row.seller_region or UNKNOWN_REGION
        sales = max(0, int(row.monthly_sales))

        market_sales[month] += sales
        market_asins[month].add(asin)
        segment_sales[(month, segment)] += sales
        segment_asins[(month, segment)].add(asin)
        region_sales[(month, region)] += sales
        region_asins[(month, region)].add(asin)
        cell_sales[(month, region, segment)] += sales
        cell_asins[(month, region, segment)].add(asin)

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
    first_month, last_month = (months[0], months[-1]) if months else ("", "")
    for month in months:
        for region in regions:
            key = (month, region)
            sales = region_sales.get(key, 0)
            count = len(region_asins.get(key, set()))
            share = _share(sales, market_sales[month])
            warnings: list[str] = []

            prev_month = _previous_month(month)
            sales_mom = share_mom = None
            if prev_month in market_sales:
                prev_sales = region_sales.get((prev_month, region), 0)
                sales_mom = _pct_change(sales, prev_sales)
                share_mom = share - _share(prev_sales, market_sales[prev_month])
            elif _month_key(first_month) < _month_key(prev_month) < _month_key(last_month):
                # A gap inside the observed range; the series start is not a gap.
                warnings.append(f"mom_base_month_missing:{prev_month}")

            prev_year = _previous_year(month)
            sales_yoy = share_yoy = None
            if prev_year in market_sales:
                prev_sales = region_sales.get((prev_year, region), 0)
                sales_yoy = _pct_change(sales, prev_sales)
                share_yoy = share - _share(prev_sales, market_sales[prev_year])
            else:
                # No YoY conclusion without the base month: flag coverage instead.
                warnings.append(f"yoy_base_month_missing:{prev_year}")

            region_metrics.append(
                SellerRegionMonthlyMetric(
                    month=month,
                    seller_region=region,
                    sales=sales,
                    asin_count=count,
                    sales_share_pct=share,
                    asin_share_pct=_share(count, len(market_asins[month])),
                    sales_mom_pct=sales_mom,
                    sales_yoy_pct=sales_yoy,
                    share_mom_pp=share_mom,
                    share_yoy_pp=share_yoy,
                    coverage_warning=";".join(warnings) or None,
                    cause_hypothesis=(
                        CAUSE_HYPOTHESIS_REGION_COVERAGE if any(w.startswith("yoy") for w in warnings) else None
                    ),
                )
            )

    region_segment_metrics: list[RegionSegmentMonthlyMetric] = []
    for month in months:
        known_total = sum(v for (m, r), v in region_sales.items() if m == month and r != UNKNOWN_REGION)
        for region in regions:
            region_total = region_sales.get((month, region), 0)
            for segment in segments:
                key = (month, region, segment)
                sales = cell_sales.get(key, 0)
                count = len(cell_asins.get(key, set()))
                region_segment_metrics.append(
                    RegionSegmentMonthlyMetric(
                        month=month,
                        seller_region=region,
                        segment=segment,
                        sales=sales,
                        asin_count=count,
                        sales_share_of_month_pct=_share(sales, market_sales[month]),
                        sales_share_of_known_region_pct=(
                            None if region == UNKNOWN_REGION else _share(sales, known_total)
                        ),
                        share_within_region_pct=_share(sales, region_total),
                        asin_share_of_month_pct=_share(count, len(market_asins[month])),
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
        base_expected = _month_range(base_start, base_end)
        current_expected = _month_range(current_start, current_end)
        missing_base = tuple(m for m in base_expected if m not in market_sales)
        missing_current = tuple(m for m in current_expected if m not in market_sales)
        if missing_base or missing_current:
            for scope, segment in [("market", None), *[("segment", s) for s in segments]]:
                period_comparisons.append(PeriodComparisonMetric(
                    scope, segment, f"{base_start}..{base_end}", f"{current_start}..{current_end}",
                    None, None, None, missing_base_months=missing_base,
                    missing_current_months=missing_current))
            continue
        base_months, current_months = base_expected, current_expected
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
        region_segments=region_segment_metrics,
        seasonality=detect_seasonality(market_sales),
    )


def _default_decomposition_pairs(months: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    available = set(months)
    for current in months:
        prior = _previous_year(current)
        if prior in available:
            pairs.append((prior, current))
    return pairs


def _month_range(start: str, end: str) -> list[str]:
    sy, sm = _month_key(start)
    ey, em = _month_key(end)
    first, last = sy * 12 + sm - 1, ey * 12 + em - 1
    if first > last:
        raise ValueError("period start must be <= end")
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(first, last + 1)]
