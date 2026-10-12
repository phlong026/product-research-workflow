from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from .models import (
    AccountTypeMetric,
    NewProductAnalysisResult,
    NewProductConfig,
    NewProductOutcome,
    NewProductGroupMetric,
    NewProductSnapshot,
    NewProductSummary,
)


def _normalize_month(value: str) -> str:
    text = str(value).strip()[:7]
    try:
        year, month = text.split("-")
        d = date(int(year), int(month), 1)
    except Exception as exc:
        raise ValueError(f"month must be YYYY-MM compatible, got {value!r}") from exc
    return f"{d.year:04d}-{d.month:02d}"


def _month_ordinal(month: str) -> int:
    year, mon = map(int, _normalize_month(month).split("-"))
    return year * 12 + mon - 1


def _month_distance(start: str, end: str) -> int:
    return _month_ordinal(end) - _month_ordinal(start) + 1


def _pct(part: float, total: float) -> float:
    return 0.0 if total == 0 else part / total * 100.0


def _normalize_account_type(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"VENDOR", "VENDOR CENTRAL", "VC"}:
        return "VC"
    if raw in {"SELLER", "SELLER CENTRAL", "SC", "3P"}:
        return "SC"
    return raw or "UNKNOWN"


def analyze_new_products(
    snapshots: Iterable[NewProductSnapshot],
    config: NewProductConfig | None = None,
) -> NewProductAnalysisResult:
    config = config or NewProductConfig()
    config.validate()

    # If the same ASIN/month appears multiple times, keep the maximum sales value.
    # This is conservative for success detection and deterministic for duplicated downloads.
    by_asin_month: dict[tuple[str, str], NewProductSnapshot] = {}
    for row in snapshots:
        if not row.eligible:
            continue
        asin = row.asin.strip().upper()
        if not asin:
            continue
        month = _normalize_month(row.month)
        normalized = NewProductSnapshot(
            asin=asin,
            month=month,
            monthly_sales=max(0, int(row.monthly_sales)),
            launch_date=row.launch_date,
            seller_region=row.seller_region or "UNKNOWN",
            account_type=_normalize_account_type(row.account_type),
            segment=row.segment or "unknown",
            eligible=True,
        )
        key = (asin, month)
        current = by_asin_month.get(key)
        if current is None or normalized.monthly_sales > current.monthly_sales:
            by_asin_month[key] = normalized

    grouped: dict[str, list[NewProductSnapshot]] = defaultdict(list)
    for row in by_asin_month.values():
        grouped[row.asin].append(row)

    all_months = sorted({row.month for row in by_asin_month.values()}, key=_month_ordinal)
    current_month = all_months[-1] if all_months else None
    survival_threshold = (
        config.sales_threshold
        if config.current_survival_threshold is None
        else config.current_survival_threshold
    )

    outcomes: list[NewProductOutcome] = []
    for asin, rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda row: _month_ordinal(row.month))
        sales_by_month = {row.month: row.monthly_sales for row in rows}
        first_observed = rows[0].month
        launch_month = None
        for row in rows:
            if row.launch_date:
                try:
                    launch_month = _normalize_month(row.launch_date)
                    break
                except ValueError:
                    pass
        anchor_month = launch_month or first_observed

        threshold_months = [
            row.month for row in rows if row.monthly_sales >= config.sales_threshold
        ]
        first_threshold = threshold_months[0] if threshold_months else None
        first_threshold_index = (
            _month_distance(anchor_month, first_threshold) if first_threshold else None
        )

        success_month = _find_success_month(sales_by_month, config)
        launch_success = success_month is not None
        current_observed = bool(current_month and current_month in sales_by_month)
        current_sales = sales_by_month.get(current_month) if current_observed else None
        current_survival = (
            bool(launch_success and current_sales is not None and current_sales >= survival_threshold)
            if current_observed
            else None
        )
        latest = rows[-1]
        outcomes.append(
            NewProductOutcome(
                asin=asin,
                anchor_month=anchor_month,
                first_observed_month=first_observed,
                first_threshold_month=first_threshold,
                first_threshold_month_index=first_threshold_index,
                launch_success=launch_success,
                success_month=success_month,
                current_month=current_month or first_observed,
                current_sales=current_sales,
                current_observed=current_observed,
                current_survival=current_survival,
                seller_region=latest.seller_region,
                account_type=latest.account_type,
                segment=latest.segment,
            )
        )

    successful = [row for row in outcomes if row.launch_success]
    surviving = [row for row in successful if row.current_survival is True]
    early = [
        row
        for row in successful
        if row.first_threshold_month_index is not None
        and row.first_threshold_month_index <= config.early_threshold_window_months
    ]
    summary = NewProductSummary(
        total_products=len(outcomes),
        successful_products=len(successful),
        success_rate_pct=_pct(len(successful), len(outcomes)),
        surviving_successful_products=len(surviving),
        survival_rate_among_successful_pct=_pct(len(surviving), len(successful)),
        successful_with_early_threshold=len(early),
        early_threshold_share_among_successful_pct=_pct(len(early), len(successful)),
        current_month=current_month,
    )

    account_types: list[AccountTypeMetric] = []
    types = sorted({row.account_type for row in outcomes})
    for account_type in types:
        all_rows = [row for row in outcomes if row.account_type == account_type]
        success_rows = [row for row in all_rows if row.launch_success]
        account_types.append(
            AccountTypeMetric(
                account_type=account_type,
                total_products=len(all_rows),
                successful_products=len(success_rows),
                success_rate_pct=_pct(len(success_rows), len(all_rows)),
                share_of_successful_pct=_pct(len(success_rows), len(successful)),
            )
        )

    def grouped_metrics(dimension: str) -> list[NewProductGroupMetric]:
        values = sorted({getattr(row, dimension) for row in outcomes})
        metrics: list[NewProductGroupMetric] = []
        for value in values:
            all_rows = [row for row in outcomes if getattr(row, dimension) == value]
            success_rows = [row for row in all_rows if row.launch_success]
            metrics.append(NewProductGroupMetric(
                dimension=dimension, value=value, total_products=len(all_rows),
                successful_products=len(success_rows), success_rate_pct=_pct(len(success_rows), len(all_rows)),
                share_of_successful_pct=_pct(len(success_rows), len(successful)),
            ))
        return metrics

    return NewProductAnalysisResult(
        outcomes=outcomes,
        summary=summary,
        account_types=account_types,
        seller_regions=grouped_metrics("seller_region"),
        segments=grouped_metrics("segment"),
        config=config,
    )


def _find_success_month(
    sales_by_month: dict[str, int], config: NewProductConfig
) -> str | None:
    months = sorted(sales_by_month, key=_month_ordinal)
    streak = 0
    previous_ordinal: int | None = None
    for month in months:
        ordinal = _month_ordinal(month)
        is_consecutive = previous_ordinal is None or ordinal == previous_ordinal + 1
        if not is_consecutive:
            streak = 0
        if sales_by_month[month] >= config.sales_threshold:
            streak += 1
        else:
            streak = 0
        if streak >= config.consecutive_months:
            return month
        previous_ordinal = ordinal
    return None
