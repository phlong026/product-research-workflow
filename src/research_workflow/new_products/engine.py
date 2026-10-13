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
    snapshots = list(snapshots)
    study_month = _normalize_month(config.study_month) if config.study_month else None
    if study_month is not None:
        input_months = {_normalize_month(row.month) for row in snapshots if row.asin.strip()}
        if input_months:
            latest = max(input_months, key=_month_ordinal)
            if latest != study_month:
                raise ValueError(
                    f"study_month {study_month} does not match the latest month in input data {latest}"
                )

    promotion_threshold = config.effective_promotion_threshold
    retention_threshold = config.effective_retention_threshold

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
            parent_asin=row.parent_asin,
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
    # Legacy "current survival" uses the retention threshold (falls back to the legacy
    # current_survival_threshold, then sales_threshold).
    survival_threshold = retention_threshold

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
            row.month for row in rows if row.monthly_sales >= promotion_threshold
        ]
        first_threshold = threshold_months[0] if threshold_months else None
        first_threshold_index = (
            _month_distance(anchor_month, first_threshold) if first_threshold else None
        )

        success_month = _find_success_month(sales_by_month, promotion_threshold, config.consecutive_months)
        launch_success_status = _streak_status(
            sales_by_month, promotion_threshold, config.consecutive_months
        )
        launch_success = launch_success_status == "yes"
        current_observed = bool(current_month and current_month in sales_by_month)
        current_sales = sales_by_month.get(current_month) if current_observed else None
        current_survival = (
            (
                None  # 成功与否未知(缺月)时,不能把存活判为 False
                if launch_success_status == "unknown"
                else bool(launch_success and current_sales is not None and current_sales >= survival_threshold)
            )
            if current_observed
            else None
        )
        window_complete = _early_window_complete(
            anchor_month, config.early_threshold_window_months, sales_by_month, current_month
        )
        window_reached = bool(
            first_threshold_index is not None
            and 1 <= first_threshold_index <= config.early_threshold_window_months
        )
        retention_status = _retention_status(
            sales_by_month, current_month, retention_threshold, config.consecutive_months
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
                early_window_complete=window_complete,
                early_window_reached=window_reached,
                retention_status=retention_status,
                launch_success_status=launch_success_status,
            )
        )

    successful = [row for row in outcomes if row.launch_success]
    success_unknown = [row for row in outcomes if row.launch_success_status == "unknown"]
    observed_successful = [row for row in successful if row.current_observed]
    surviving = [row for row in successful if row.current_survival is True]
    early = [
        row
        for row in successful
        if row.first_threshold_month_index is not None
        and 1 <= row.first_threshold_month_index <= config.early_threshold_window_months
    ]
    retained = [row for row in outcomes if row.retention_status == "retained"]
    retention_unknown = [row for row in outcomes if row.retention_status == "unknown"]
    window_complete_rows = [row for row in outcomes if row.early_window_complete]
    window_reached_rows = [row for row in window_complete_rows if row.early_window_reached]
    summary = NewProductSummary(
        total_products=len(outcomes),
        successful_products=len(successful),
        success_rate_pct=_pct(len(successful), len(outcomes)),
        launch_success_unknown_products=len(success_unknown),
        success_rate_upper_bound_pct=_pct(len(successful) + len(success_unknown), len(outcomes)),
        surviving_successful_products=len(surviving),
        survival_rate_among_successful_pct=(_pct(len(surviving), len(observed_successful)) if observed_successful else None),
        observed_successful_products=len(observed_successful),
        unknown_successful_products=len(successful) - len(observed_successful),
        current_observation_coverage_pct=(_pct(len(observed_successful), len(successful)) if successful else None),
        successful_with_early_threshold=len(early),
        early_threshold_share_among_successful_pct=_pct(len(early), len(successful)),
        current_month=current_month,
        promotion_waist_sales_threshold=promotion_threshold,
        retention_sales_threshold=retention_threshold,
        retention_denominator_products=len(outcomes),
        retained_products=len(retained),
        retention_unknown_products=len(retention_unknown),
        retention_rate_pct=_pct(len(retained), len(outcomes)),
        retention_rate_upper_bound_pct=_pct(len(retained) + len(retention_unknown), len(outcomes)),
        retention_denominator_note=(
            f"分母=全部有效新品({len(outcomes)});分子=连续满{config.consecutive_months}个月"
            f"(月销量>={retention_threshold})且研究当月({current_month})仍>={retention_threshold}。"
            f"研究当月无观测、或历史中间缺月导致无法判定连续条件的新品记为未知观测({len(retention_unknown)}个),"
            "不按0处理:下限不计入留存,上限全部计入留存。"
        ),
        early_window_complete_products=len(window_complete_rows),
        early_window_incomplete_products=len(outcomes) - len(window_complete_rows),
        early_window_reached_products=len(window_reached_rows),
        early_window_reached_share_pct=_pct(len(window_reached_rows), len(window_complete_rows)),
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
    sales_by_month: dict[str, int], threshold: int, consecutive_months: int
) -> str | None:
    months = sorted(sales_by_month, key=_month_ordinal)
    streak = 0
    previous_ordinal: int | None = None
    for month in months:
        ordinal = _month_ordinal(month)
        is_consecutive = previous_ordinal is None or ordinal == previous_ordinal + 1
        if not is_consecutive:
            streak = 0
        if sales_by_month[month] >= threshold:
            streak += 1
        else:
            streak = 0
        if streak >= consecutive_months:
            return month
        previous_ordinal = ordinal
    return None


def _early_window_complete(
    anchor_month: str,
    window_months: int,
    sales_by_month: dict[str, int],
    current_month: str | None,
) -> bool:
    """Natural months anchor..anchor+window-1 must all be observed and not after the study month."""
    start = _month_ordinal(anchor_month)
    if current_month is None or start + window_months - 1 > _month_ordinal(current_month):
        return False
    return all(
        _ordinal_to_month(start + offset) in sales_by_month for offset in range(window_months)
    )


def _ordinal_to_month(ordinal: int) -> str:
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def _streak_status(sales_by_month: dict[str, int], threshold: int, consecutive_months: int) -> str:
    """yes / no / unknown:是否存在连续 ``consecutive_months`` 个月 >= threshold。

    缺月 = 未知(不当 0)。只有首个与最后一个观测月之间的"中间缺月"可能补足连续段:
    yes = 仅凭已观测月份就满足;unknown = 已观测月份不满足,但把中间缺月当作达标即可满足;
    no = 即使缺月全部达标也凑不齐(已被观测月份证伪)。
    """
    if _find_success_month(sales_by_month, threshold, consecutive_months) is not None:
        return "yes"
    ordinals = sorted(_month_ordinal(m) for m in sales_by_month)
    observed = {_month_ordinal(m): v for m, v in sales_by_month.items()}
    streak = 0
    for ordinal in range(ordinals[0], ordinals[-1] + 1):
        sales = observed.get(ordinal)
        # 缺月(sales is None)乐观地视为达标;已观测且未达标则中断连续段
        streak = streak + 1 if sales is None or sales >= threshold else 0
        if streak >= consecutive_months:
            return "unknown"
    return "no"


def _retention_status(
    sales_by_month: dict[str, int],
    current_month: str | None,
    threshold: int,
    consecutive_months: int,
) -> str:
    """retained / not_retained / unknown.

    retained: a run of ``consecutive_months`` consecutive months >= threshold exists AND the
    current month is observed and >= threshold. unknown: the condition cannot be falsified by
    observed months -- either the current month has no observation while the run exists/could
    exist, or interior missing months could complete the run (missing is never treated as 0).
    not_retained: falsified by observed months (no run possible even if gaps qualify, or the
    observed current month is below threshold).
    """
    streak = _streak_status(sales_by_month, threshold, consecutive_months)
    if streak == "no":
        return "not_retained"
    if current_month is None or current_month not in sales_by_month:
        return "unknown"
    if sales_by_month[current_month] < threshold:
        return "not_retained"
    return "retained" if streak == "yes" else "unknown"
