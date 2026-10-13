from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from .models import SeasonalityResult, SeasonalityYear

CAUSE_HYPOTHESIS_COVERAGE = (
    "待验证假设:该年份月份不全,年内高低与同比变化可能反映数据覆盖差异而非真实季节波动;未经验证"
)


def _parse(month: str) -> tuple[int, int]:
    year, mon = month.strip()[:7].split("-")
    if not 1 <= int(mon) <= 12:
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return int(year), int(mon)


def detect_seasonality(monthly_sales: Mapping[str, int]) -> SeasonalityResult:
    """Identify the high/low month of each calendar year from a monthly series.

    Deterministic, tie-break = earlier month. A year is "complete" only when all
    12 months are present; incomplete years get ``coverage_warning`` and NO
    high/low conclusion, so a missing month can never be mistaken for a seasonal
    low. ``recurring_*_month`` requires the same month-of-year to be the
    high/low of every complete year and at least two complete years.
    """
    by_year: dict[int, dict[int, int]] = defaultdict(dict)
    for key, value in monthly_sales.items():
        year, mon = _parse(key)
        by_year[year][mon] = int(value)

    years: list[SeasonalityYear] = []
    complete_highs: list[int] = []
    complete_lows: list[int] = []
    for year in sorted(by_year):
        observed = by_year[year]
        missing = [f"{year:04d}-{m:02d}" for m in range(1, 13) if m not in observed]
        if missing:
            years.append(
                SeasonalityYear(
                    year=year,
                    months_observed=len(observed),
                    missing_months=missing,
                    is_complete=False,
                    coverage_warning=f"incomplete_year:{len(observed)}/12_months_observed",
                    cause_hypothesis=CAUSE_HYPOTHESIS_COVERAGE,
                )
            )
            continue
        high = min(observed, key=lambda m: (-observed[m], m))
        low = min(observed, key=lambda m: (observed[m], m))
        complete_highs.append(high)
        complete_lows.append(low)
        years.append(
            SeasonalityYear(
                year=year,
                months_observed=12,
                missing_months=[],
                is_complete=True,
                high_month=f"{year:04d}-{high:02d}",
                high_sales=observed[high],
                low_month=f"{year:04d}-{low:02d}",
                low_sales=observed[low],
            )
        )

    complete_years = [y.year for y in years if y.is_complete]
    recurring_high = recurring_low = None
    if len(complete_years) >= 2:
        if len(set(complete_highs)) == 1:
            recurring_high = f"{complete_highs[0]:02d}"
        if len(set(complete_lows)) == 1:
            recurring_low = f"{complete_lows[0]:02d}"

    incomplete = [y.year for y in years if not y.is_complete]
    warning = (
        f"incomplete_years:{','.join(str(y) for y in incomplete)}" if incomplete else None
    )
    return SeasonalityResult(
        years=years,
        complete_years=complete_years,
        recurring_high_month=recurring_high,
        recurring_low_month=recurring_low,
        coverage_warning=warning,
    )
