from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from research_workflow.classification.models import ProductClassification
from research_workflow.data.models import BSRRecord

from .market import _classification_label, _month_key

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%b %d, %Y", "%B %d, %Y")
_MONTH_FORMATS = ("%Y-%m", "%Y/%m")
# Fixed reason order: used to pick one reason for an ASIN with no valid date.
_REASONS = ("missing", "unparseable", "future", "too_early")

NOTES = (
    "earliest_launch_date is derived from launch_date of valid records only; abnormal "
    "dates (missing / unparseable / future / earlier than min_launch_date) are excluded.",
    "earliest_observed_in_data is the first month the segment appears in the supplied "
    "data window. It is NOT a launch date and must never be read as when the segment began.",
    "Month-only launch dates (YYYY-MM) are normalised to the first day of that month.",
)


@dataclass(slots=True, frozen=True)
class IterationConfig:
    """Validity rules for launch dates. ``as_of`` defaults to today (set it for reproducible runs)."""

    min_launch_date: date = date(1990, 1, 1)
    as_of: date | None = None


@dataclass(slots=True, frozen=True)
class SegmentIteration:
    segment: str
    order: int | None  # 1 = earliest valid launch date; None = no valid launch date
    earliest_launch_date: str | None  # YYYY-MM-DD, from launch_date only
    earliest_launch_asin: str | None
    earliest_observed_in_data: str  # YYYY-MM, first month seen in data; NOT a launch date
    asin_count: int
    valid_launch_date_asin_count: int
    excluded_missing: int
    excluded_unparseable: int
    excluded_future: int
    excluded_too_early: int


@dataclass(slots=True)
class IterationResult:
    segments: list[SegmentIteration]
    notes: tuple[str, ...] = NOTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [asdict(row) for row in self.segments],
            "notes": list(self.notes),
        }


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    # Second candidate drops a time part such as "2020-01-05 00:00:00" / "2020-01-05T..".
    for candidate in (text, text[:10]):
        for fmt in _DATE_FORMATS + _MONTH_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _check(value: str | None, min_date: date, as_of: date) -> tuple[date | None, str | None]:
    """Return (valid_date, None) or (None, reason)."""
    if not (value or "").strip():
        return None, "missing"
    parsed = _parse_date(value)
    if parsed is None:
        return None, "unparseable"
    if parsed > as_of:
        return None, "future"
    if parsed < min_date:
        return None, "too_early"
    return parsed, None


def build_iteration_comparison(
    records: Iterable[BSRRecord],
    classifications: Mapping[str, str | ProductClassification],
    *,
    config: IterationConfig | None = None,
) -> IterationResult:
    """Per-segment earliest valid launch date, ordered earliest first.

    Segments without any valid launch date are listed last with ``order=None``
    (their ``earliest_observed_in_data`` is still reported, but never promoted
    to a launch date). Ties on the earliest date break by segment name.
    """
    cfg = config or IterationConfig()
    as_of = cfg.as_of or date.today()

    asin_segment: dict[str, str] = {}
    asin_valid: dict[str, list[date]] = defaultdict(list)
    asin_reasons: dict[str, set[str]] = defaultdict(set)
    segment_first_month: dict[str, tuple[int, int]] = {}

    for row in records:
        asin = row.asin.upper()
        segment = _classification_label(classifications.get(asin))
        asin_segment.setdefault(asin, segment)
        month = _month_key(row.month)
        if segment not in segment_first_month or month < segment_first_month[segment]:
            segment_first_month[segment] = month
        valid, reason = _check(row.launch_date, cfg.min_launch_date, as_of)
        if valid is not None:
            asin_valid[asin].append(valid)
        elif reason is not None:
            asin_reasons[asin].add(reason)

    per_segment: dict[str, dict[str, Any]] = {
        seg: {"asins": 0, "valid": 0, "best": None, "reasons": dict.fromkeys(_REASONS, 0)}
        for seg in segment_first_month
    }
    for asin, segment in asin_segment.items():
        slot = per_segment[segment]
        slot["asins"] += 1
        if asin_valid.get(asin):
            slot["valid"] += 1
            candidate = (min(asin_valid[asin]), asin)
            if slot["best"] is None or candidate < slot["best"]:
                slot["best"] = candidate
        else:
            reason = next(r for r in _REASONS if r in asin_reasons[asin])
            slot["reasons"][reason] += 1

    rows: list[SegmentIteration] = []
    for segment, slot in per_segment.items():
        best = slot["best"]
        year, mon = segment_first_month[segment]
        rows.append(
            SegmentIteration(
                segment=segment,
                order=None,
                earliest_launch_date=best[0].isoformat() if best else None,
                earliest_launch_asin=best[1] if best else None,
                earliest_observed_in_data=f"{year:04d}-{mon:02d}",
                asin_count=slot["asins"],
                valid_launch_date_asin_count=slot["valid"],
                excluded_missing=slot["reasons"]["missing"],
                excluded_unparseable=slot["reasons"]["unparseable"],
                excluded_future=slot["reasons"]["future"],
                excluded_too_early=slot["reasons"]["too_early"],
            )
        )

    rows.sort(key=lambda r: (r.earliest_launch_date is None, r.earliest_launch_date or "", r.segment))
    ordered = [
        replace(r, order=i + 1 if r.earliest_launch_date else None) for i, r in enumerate(rows)
    ]
    return IterationResult(segments=ordered)
