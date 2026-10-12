from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(slots=True, frozen=True)
class MetricCheck:
    key: str
    actual: float
    expected: float
    tolerance: float
    passed: bool


def compare_metrics(
    actual: Mapping[str, float],
    expected: Mapping[str, float],
    tolerances: Mapping[str, float] | None = None,
) -> list[MetricCheck]:
    """Compare computed integration metrics with a frozen Golden Case baseline.

    This helper deliberately receives *computed* values; it never substitutes
    baseline values into production calculations.
    """
    tolerances = tolerances or {}
    checks: list[MetricCheck] = []
    for key, exp in expected.items():
        if key not in actual:
            continue
        tol = float(tolerances.get(key, 0.01))
        value = float(actual[key])
        checks.append(
            MetricCheck(
                key=key,
                actual=value,
                expected=float(exp),
                tolerance=tol,
                passed=abs(value - float(exp)) <= tol,
            )
        )
    return checks
