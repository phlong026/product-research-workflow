from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .bsr_pipeline import validate_month


@dataclass(slots=True, frozen=True)
class MonthCoverage:
    expected: list[str]
    present: list[str]
    missing: list[str]
    extra: list[str]

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": list(self.expected),
            "present": list(self.present),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "complete": self.complete,
        }


def available_month_window(
    today: date, years: int = 3, latest_available_lag_months: int = 1
) -> list[str]:
    """Return the ascending YYYY-MM window of `years * 12` months ending at the latest available month.

    The latest available month is `today` minus `latest_available_lag_months` calendar months
    (data for the current month is normally not final yet).
    """
    if years < 1:
        raise ValueError("years must be >= 1")
    if latest_available_lag_months < 0:
        raise ValueError("latest_available_lag_months must be >= 0")
    latest_index = today.year * 12 + (today.month - 1) - latest_available_lag_months
    first_index = latest_index - years * 12 + 1
    return [f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(first_index, latest_index + 1)]


def month_coverage_report(expected: list[str], present: Iterable[str]) -> MonthCoverage:
    """Compare expected months with months actually present; missing months are listed explicitly."""
    expected_months = sorted({validate_month(m) for m in expected})
    present_months = sorted({validate_month(m) for m in present})
    present_set = set(present_months)
    expected_set = set(expected_months)
    return MonthCoverage(
        expected=expected_months,
        present=present_months,
        missing=[m for m in expected_months if m not in present_set],
        extra=[m for m in present_months if m not in expected_set],
    )
