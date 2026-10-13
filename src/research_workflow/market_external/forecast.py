"""US market-size extrapolation with explicit scope layers (R15).

A series measured at one scope (e.g. this system's Top100 sample) may only be
reported at a wider scope through an explicit, sourced ``CoverageAssumption``;
otherwise ``extrapolate`` refuses. All outputs are forecasts and carry their
base period, scope, method and assumptions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from ._math import cagr, compound


class ScopeLevel(str, Enum):
    TOP_SAMPLE = "top_sample"  # this system's Top100 sample sales
    AMAZON_CHANNEL = "amazon_channel"
    FULL_MARKET = "full_market"


_SCOPE_RANK = {ScopeLevel.TOP_SAMPLE: 0, ScopeLevel.AMAZON_CHANNEL: 1, ScopeLevel.FULL_MARKET: 2}
_SCENARIO_NAMES = ("low", "mid", "high")


@dataclass(frozen=True)
class SeriesPoint:
    period: str  # "YYYY" or "YYYY-MM"; one series must use a single granularity
    value: float


@dataclass(frozen=True)
class CoverageAssumption:
    """value_at_from_scope / value_at_to_scope = ratio (0 < ratio <= 1); must cite a source."""

    ratio: float
    source: str
    from_scope: ScopeLevel
    to_scope: ScopeLevel

    def __post_init__(self) -> None:
        if not 0 < self.ratio <= 1:
            raise ValueError("coverage ratio must be in (0, 1]")
        if not self.source.strip():
            raise ValueError("coverage ratio needs a source")
        if _SCOPE_RANK[self.to_scope] <= _SCOPE_RANK[self.from_scope]:
            raise ValueError("coverage can only widen scope (sample -> channel -> market)")


@dataclass(frozen=True)
class ForecastPoint:
    period: str
    value: float


@dataclass(frozen=True)
class ForecastResult:
    scope: ScopeLevel  # scope of the reported values
    source_scope: ScopeLevel  # scope the input series was measured at
    method: str
    unit: str
    base_period: str
    base_value: float  # at `scope`
    horizon: str
    scenarios: dict[str, tuple[ForecastPoint, ...]]  # low/mid/high -> points
    assumptions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    coverage: CoverageAssumption | None = None
    is_forecast: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _months(period: str) -> int:
    return int(period[:4]) * 12 + int(period[5:7]) - 1


def _month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def extrapolate(
    series: Sequence[SeriesPoint],
    scope: ScopeLevel,
    *,
    method: str = "cagr",
    horizon_years: int | None = None,
    horizon_months: int | None = None,
    scenarios: Mapping[str, float] | None = None,
    target_scope: ScopeLevel | None = None,
    coverage: CoverageAssumption | None = None,
    evidence_ids: Sequence[str] = (),
    unit: str = "units",
) -> ForecastResult:
    """Project ``series`` (measured at ``scope``) forward.

    method="cagr": mid = first->last CAGR; scenarios are annual growth rates.
    method="linear": mid = (last-first)/periods per period; scenarios are slopes in
    units per year (in ``target_scope`` units). ``scenarios`` accepts only "low"/"high".
    """
    scope = ScopeLevel(scope)
    target = ScopeLevel(target_scope) if target_scope is not None else scope
    if method not in ("cagr", "linear"):
        raise ValueError("method must be 'cagr' or 'linear'")
    if (horizon_years is None) == (horizon_months is None):
        raise ValueError("give exactly one of horizon_years / horizon_months")
    if len(series) < 2:
        raise ValueError("need at least two points to extrapolate")
    unknown = set(scenarios or {}) - {"low", "high"}
    if unknown:
        raise ValueError(f"unknown scenario names {sorted(unknown)}; mid is derived from the method")

    # Scope guard: never present sample sales as channel/market totals without a sourced coverage ratio.
    scale = 1.0
    if target != scope:
        if _SCOPE_RANK[target] < _SCOPE_RANK[scope]:
            raise ValueError("cannot narrow scope by extrapolation")
        if coverage is None:
            raise ValueError(
                f"{scope.value} cannot be extrapolated to {target.value} without a sourced coverage_ratio"
            )
        if (coverage.from_scope, coverage.to_scope) != (scope, target):
            raise ValueError("coverage assumption does not match source/target scope")
        scale = 1 / coverage.ratio

    ordered = sorted(series, key=lambda p: p.period)
    annual = len(ordered[0].period) == 4
    if any((len(p.period) == 4) != annual for p in ordered):
        raise ValueError("series mixes annual and monthly periods")
    if len({p.period for p in ordered}) != len(ordered):
        raise ValueError("duplicate periods in series")
    if annual and horizon_months is not None:
        raise ValueError("annual series needs horizon_years")
    steps = horizon_years if annual else (horizon_months if horizon_months is not None else horizon_years * 12)
    if steps is None or steps < 1:
        raise ValueError("horizon must be positive")

    first, last = ordered[0], ordered[-1]
    span = (int(last.period) - int(first.period)) if annual else (_months(last.period) - _months(first.period))
    per_year = 1 if annual else 12
    first_value, last_value = first.value * scale, last.value * scale

    if method == "cagr":
        mid_rate = cagr(first_value, last_value, span)
        rates = {"mid": mid_rate}
        rates.update({k: (1 + v) ** (1 / per_year) - 1 for k, v in (scenarios or {}).items()})
        project = lambda rate, k: compound(last_value, rate, k)  # noqa: E731
        mid_text = f"mid = historical CAGR {first.period}->{last.period} = {(1 + mid_rate) ** per_year - 1:.6f} per year"
    else:
        rates = {"mid": (last_value - first_value) / span}
        rates.update({k: v / per_year for k, v in (scenarios or {}).items()})
        project = lambda rate, k: last_value + rate * k  # noqa: E731
        mid_text = f"mid = linear slope {first.period}->{last.period} = {rates['mid'] * per_year:.6f} units per year"

    def label(k: int) -> str:
        return str(int(last.period) + k) if annual else _month_label(_months(last.period) + k)

    out = {
        name: tuple(ForecastPoint(label(k), project(rates[name], k)) for k in range(1, steps + 1))
        for name in _SCENARIO_NAMES
        if name in rates
    }
    if not ("low" in out and "high" in out):
        scenario_note = "no complete low/high scenarios supplied: interval not produced"
    else:
        scenario_note = "low/high scenario rates supplied by caller, not inferred"
    assumptions = [
        f"method={method}",
        f"base = last observed point {last.period} at scope {scope.value}",
        mid_text,
        scenario_note,
        "extrapolation only; no saturation, price, seasonality or macro effects",
    ]
    if coverage is not None and target != scope:
        assumptions.append(
            f"scope widened {scope.value}->{target.value} via coverage_ratio={coverage.ratio} (source: {coverage.source})"
        )
    return ForecastResult(
        scope=target,
        source_scope=scope,
        method=method,
        unit=unit,
        base_period=last.period,
        base_value=last_value,
        horizon=f"{steps} {'years' if annual else 'months'}",
        scenarios=out,
        assumptions=tuple(assumptions),
        evidence_ids=tuple(evidence_ids),
        coverage=coverage if target != scope else None,
    )
