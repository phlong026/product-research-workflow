"""TikTok channel unit sales: shares, 2026 monthly, CAGR and CAGR extrapolation (R17).

All quantities are units (台). Input is hand-curated CSV/JSON rows; any field whose
name contains "gmv" is rejected so revenue-based numbers cannot leak into unit maths.
Every number carries an ``evidence_id`` pointing at the evidence ledger, and every
projected value is flagged ``is_forecast=True`` with its assumptions.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ._math import cagr, compound

GEOS = ("global", "us")
SEGMENTS = ("total", "countertop")
CHANNELS = ("all", "tiktok")
_UNIT_LABELS = ("units", "台")
_REQUIRED = ("geo", "segment", "channel", "period", "units", "is_forecast", "evidence_id")
METHOD_CAGR = "cagr_extrapolation"


@dataclass(frozen=True)
class UnitsRecord:
    geo: str
    segment: str
    channel: str
    period: str  # "YYYY" (annual) or "YYYY-MM" (monthly)
    units: float
    is_forecast: bool
    evidence_id: str


@dataclass(frozen=True)
class AnnualValue:
    units: float
    is_forecast: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ShareResult:
    period: str
    status: str  # ok | insufficient_evidence
    share: float | None  # tiktok units / total units
    tiktok_units: float | None
    total_units: float | None
    is_forecast: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class MonthlyPoint:
    month: str
    units: float
    is_forecast: bool
    evidence_id: str


@dataclass(frozen=True)
class CagrResult:
    status: str  # ok | insufficient_evidence
    start_year: int
    end_year: int
    start_units: float | None
    end_units: float | None
    cagr: float | None
    is_forecast: bool  # True when the end (or start) value is itself a forecast
    method: str
    evidence_ids: tuple[str, ...]
    missing_reason: str = ""


@dataclass(frozen=True)
class ProjectionPoint:
    year: int
    scenario: str  # low | mid | high
    units: float
    growth_rate: float
    is_forecast: bool = True


@dataclass(frozen=True)
class ProjectionResult:
    status: str  # ok | insufficient_evidence
    method: str
    base_year: int
    base_units: float | None
    growth_rate_source: str
    points: tuple[ProjectionPoint, ...]
    assumptions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    is_forecast: bool = True


@dataclass(frozen=True)
class TikTokAnalysis:
    geo: str
    segment: str
    unit: str
    shares: tuple[ShareResult, ...]
    monthly_2026: tuple[MonthlyPoint, ...]
    missing_months_2026: tuple[str, ...]
    cagr_2024_2026: CagrResult
    projection: ProjectionResult
    annual: tuple[tuple[int, AnnualValue], ...]  # observed/declared TikTok annual units

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- input

def _reject_gmv(names: Iterable[str]) -> None:
    bad = sorted(n for n in names if "gmv" in str(n).lower())
    if bad:
        raise ValueError(f"GMV fields are not allowed (units only): {bad}")


def _as_bool(raw: Any, index: int) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValueError(f"row {index}: is_forecast must be explicit true/false, got {raw!r}")


def parse_units_rows(rows: Iterable[Mapping[str, Any]]) -> list[UnitsRecord]:
    records: list[UnitsRecord] = []
    for index, row in enumerate(rows, start=1):
        _reject_gmv(row.keys())
        missing = [c for c in _REQUIRED if row.get(c) in (None, "")]
        if missing:
            raise ValueError(f"row {index}: missing columns {missing}")
        label = row.get("unit")
        if label not in (None, "") and str(label).strip() not in _UNIT_LABELS:
            raise ValueError(f"row {index}: unit must be units/台, got {label!r}")
        _reject_gmv([str(label or ""), str(row["evidence_id"])])
        geo, segment, channel = (str(row[k]).strip() for k in ("geo", "segment", "channel"))
        for value, allowed, name in ((geo, GEOS, "geo"), (segment, SEGMENTS, "segment"), (channel, CHANNELS, "channel")):
            if value not in allowed:
                raise ValueError(f"row {index}: {name} must be one of {allowed}, got {value!r}")
        period = str(row["period"]).strip()
        if not (len(period) == 4 and period.isdigit()) and not (
            len(period) == 7 and period[4] == "-" and period[:4].isdigit() and period[5:].isdigit()
        ):
            raise ValueError(f"row {index}: period must be YYYY or YYYY-MM, got {period!r}")
        try:
            units = float(row["units"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"row {index}: units must be numeric, got {row['units']!r}") from exc
        if units < 0:
            raise ValueError(f"row {index}: units must be >= 0")
        records.append(
            UnitsRecord(geo, segment, channel, period, units, _as_bool(row["is_forecast"], index),
                        str(row["evidence_id"]).strip())
        )
    return records


def load_units_file(path: str | Path) -> list[UnitsRecord]:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data["records"] if isinstance(data, dict) else data
    else:
        reader = csv.DictReader(io.StringIO(text))
        _reject_gmv(reader.fieldnames or [])
        rows = list(reader)
    return parse_units_rows(rows)


# ---------------------------------------------------------------- calculations

def _select(records: Iterable[UnitsRecord], geo: str, segment: str, channel: str) -> list[UnitsRecord]:
    return [r for r in records if (r.geo, r.segment, r.channel) == (geo, segment, channel)]


def annual_units(records: Iterable[UnitsRecord], geo: str, segment: str, channel: str, year: int) -> AnnualValue | None:
    """Annual value: a declared annual record, else the sum of all 12 monthly records, else None."""
    chosen = _select(records, geo, segment, channel)
    annual = [r for r in chosen if r.period == str(year)]
    if annual:
        r = annual[0]
        return AnnualValue(r.units, r.is_forecast, (r.evidence_id,))
    months = {r.period: r for r in chosen if r.period.startswith(f"{year}-")}
    if len(months) == 12:
        ordered = [months[f"{year}-{m:02d}"] for m in range(1, 13)]
        return AnnualValue(
            sum(r.units for r in ordered),
            any(r.is_forecast for r in ordered),
            tuple(r.evidence_id for r in ordered),
        )
    return None


def _period_units(records: list[UnitsRecord], geo: str, segment: str, channel: str, period: str) -> AnnualValue | None:
    if len(period) == 4:
        return annual_units(records, geo, segment, channel, int(period))
    for r in _select(records, geo, segment, channel):
        if r.period == period:
            return AnnualValue(r.units, r.is_forecast, (r.evidence_id,))
    return None


def tiktok_share(records: Iterable[UnitsRecord], geo: str, segment: str, period: str) -> ShareResult:
    """TikTok share = TikTok units / total units for the same geo, segment and period."""
    recs = list(records)
    tiktok = _period_units(recs, geo, segment, "tiktok", period)
    total = _period_units(recs, geo, segment, "all", period)
    if tiktok is None or total is None or total.units <= 0:
        return ShareResult(period, "insufficient_evidence", None,
                           tiktok.units if tiktok else None, total.units if total else None,
                           False, (tiktok.evidence_ids if tiktok else ()) + (total.evidence_ids if total else ()))
    return ShareResult(period, "ok", tiktok.units / total.units, tiktok.units, total.units,
                       tiktok.is_forecast or total.is_forecast, tiktok.evidence_ids + total.evidence_ids)


def monthly_2026(records: Iterable[UnitsRecord], geo: str, segment: str, channel: str = "tiktok") -> tuple[tuple[MonthlyPoint, ...], tuple[str, ...]]:
    by_month = {r.period: r for r in _select(records, geo, segment, channel) if r.period.startswith("2026-")}
    points, missing = [], []
    for m in range(1, 13):
        key = f"2026-{m:02d}"
        r = by_month.get(key)
        if r is None:
            missing.append(key)
        else:
            points.append(MonthlyPoint(key, r.units, r.is_forecast, r.evidence_id))
    return tuple(points), tuple(missing)


def cagr_2024_2026(records: Iterable[UnitsRecord], geo: str, segment: str, channel: str = "tiktok") -> CagrResult:
    """CAGR = (v2026 / v2024) ** (1 / 2) - 1."""
    recs = list(records)
    start = annual_units(recs, geo, segment, channel, 2024)
    end = annual_units(recs, geo, segment, channel, 2026)
    method = "CAGR=(v2026/v2024)^(1/2)-1"
    if start is None or end is None or start.units <= 0:
        reasons = []
        if start is None or start.units <= 0:
            reasons.append("missing or non-positive 2024 value")
        if end is None:
            reasons.append("missing 2026 value")
        reason = "; ".join(reasons)
        return CagrResult("insufficient_evidence", 2024, 2026, start.units if start else None,
                          end.units if end else None, None, False, method,
                          (start.evidence_ids if start else ()) + (end.evidence_ids if end else ()), reason)
    return CagrResult("ok", 2024, 2026, start.units, end.units, cagr(start.units, end.units, 2),
                      start.is_forecast or end.is_forecast, method, start.evidence_ids + end.evidence_ids)


def project_2026_2028(
    records: Iterable[UnitsRecord],
    geo: str,
    segment: str,
    *,
    channel: str = "tiktok",
    high_growth: float | None = None,
    low_growth: float | None = None,
) -> ProjectionResult:
    """Extrapolate 2027-2028 from the 2026 base at the 2024-2026 CAGR (mid) and given low/high rates."""
    recs = list(records)
    growth = cagr_2024_2026(recs, geo, segment, channel)
    base = annual_units(recs, geo, segment, channel, 2026)
    source = "cagr_2024_2026 (mid); low/high growth rates supplied by caller"
    if growth.status != "ok" or base is None:
        return ProjectionResult("insufficient_evidence", METHOD_CAGR, 2026, None, source, (),
                                ("evidence insufficient: no conclusion produced; " + growth.missing_reason,),
                                growth.evidence_ids)
    rates = {"mid": growth.cagr}
    if low_growth is not None:
        rates["low"] = low_growth
    if high_growth is not None:
        rates["high"] = high_growth
    if "low" in rates and "high" in rates and not rates["low"] <= rates["mid"] <= rates["high"]:
        raise ValueError("scenario growth rates must satisfy low <= mid(CAGR) <= high")
    points = tuple(
        ProjectionPoint(2026 + n, name, compound(base.units, rate, n), rate)
        for name, rate in sorted(rates.items(), key=lambda kv: ("low", "mid", "high").index(kv[0]))
        for n in (1, 2)
    )
    assumptions = [
        f"method={METHOD_CAGR}",
        f"base year 2026 = {base.units} units" + (" (itself a forecast)" if base.is_forecast else ""),
        f"mid growth rate = 2024-2026 CAGR = {growth.cagr:.6f}",
        "constant annual growth, no saturation, price or seasonality effects",
    ]
    if "low" not in rates or "high" not in rates:
        assumptions.append("no complete low/high scenario supplied: interval not produced")
    return ProjectionResult("ok", METHOD_CAGR, 2026, base.units, source, points, tuple(assumptions),
                            tuple(dict.fromkeys(base.evidence_ids + growth.evidence_ids)))


def analyze_tiktok(
    records: Iterable[UnitsRecord],
    *,
    geo: str,
    segment: str,
    high_growth: float | None = None,
    low_growth: float | None = None,
) -> TikTokAnalysis:
    recs = list(records)
    periods = sorted({r.period for r in recs if (r.geo, r.segment) == (geo, segment)})
    shares = tuple(s for s in (tiktok_share(recs, geo, segment, p) for p in periods) if s.status == "ok")
    months, missing = monthly_2026(recs, geo, segment)
    annual = tuple(
        (y, v) for y in range(2024, 2027) if (v := annual_units(recs, geo, segment, "tiktok", y)) is not None
    )
    return TikTokAnalysis(
        geo=geo,
        segment=segment,
        unit="units",
        shares=shares,
        monthly_2026=months,
        missing_months_2026=missing,
        cagr_2024_2026=cagr_2024_2026(recs, geo, segment),
        projection=project_2026_2028(recs, geo, segment, high_growth=high_growth, low_growth=low_growth),
        annual=annual,
    )
