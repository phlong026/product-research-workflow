"""New-product queue (T3-3): which ASINs count as "new" in each crawl month.

Definitions
-----------
- Crawl month: a month that appears in the input rows. Its reference date is the month end.
- An ASIN is *new* in a crawl month when ``0 <= month_end - launch_date <= window_days``.
- Natural months only: the launch date's own month is the first month; a month end is the
  last calendar day of the month.
- Old-parent variant ("老带新"): the ASIN's ``parent_asin`` already existed in an earlier month
  (another ASIN with the same parent, or the parent ASIN itself, was observed before this
  ASIN's first observed month). History is limited to the rows supplied: callers must pass
  enough earlier months for the detection to be meaningful.
- Manual eligibility beats automatic judgement: ``manual_eligible[asin] = True/False`` (or a
  snapshot with ``eligible=False``, treated as a manual exclusion) overrides old-parent logic.
- Coverage: a launch month M is covered when some crawl month-end E satisfies
  ``E >= end(M)`` and ``E - start(M) <= window_days``, i.e. every product launched in M is
  still within the window at that crawl. Uncovered months are reported in ``coverage_gaps``.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from .engine import _month_ordinal, _normalize_month, _ordinal_to_month
from .models import NewProductSnapshot


@dataclass(slots=True, frozen=True)
class NewProductQueueItem:
    asin: str
    parent_asin: str
    launch_date: str
    launch_month: str
    first_new_month: str
    new_months: tuple[str, ...]
    old_parent_variant: bool = False
    manually_included: bool = False


@dataclass(slots=True, frozen=True)
class QueueExclusion:
    asin: str
    month: str
    reason: str  # old_parent / manual_ineligible / duplicate / no_launch_date
    detail: str = ""


@dataclass(slots=True, frozen=True)
class MonthCoverage:
    month: str
    quarter: str
    covered: bool
    covering_crawl_month: str | None
    crosses_quarter_boundary: bool
    new_product_count: int


@dataclass(slots=True)
class NewProductQueue:
    study_month: str
    window_days: int
    crawl_months: list[str]
    items: list[NewProductQueueItem]
    exclusions: list[QueueExclusion]
    coverage: list[MonthCoverage]
    coverage_gaps: list[str] = field(default_factory=list)

    def excluded_asins(self, reason: str) -> list[str]:
        return sorted({e.asin for e in self.exclusions if e.reason == reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_month": self.study_month,
            "window_days": self.window_days,
            "crawl_months": list(self.crawl_months),
            "items": [asdict(row) for row in self.items],
            "exclusions": [asdict(row) for row in self.exclusions],
            "coverage": [asdict(row) for row in self.coverage],
            "coverage_gaps": list(self.coverage_gaps),
        }


def _month_end(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, calendar.monthrange(year, mon)[1])


def _month_start(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1)


def _quarter(month: str) -> str:
    year, mon = map(int, month.split("-"))
    return f"{year:04d}-Q{(mon - 1) // 3 + 1}"


def _parse_launch_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def build_new_product_queue(
    rows: Iterable[NewProductSnapshot],
    *,
    study_month: str,
    window_days: int = 90,
    manual_eligible: Mapping[str, bool] | None = None,
) -> NewProductQueue:
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    study_month = _normalize_month(study_month)
    manual = {str(k).strip().upper(): bool(v) for k, v in (manual_eligible or {}).items()}

    normalized = [
        (row.asin.strip().upper(), _normalize_month(row.month), row)
        for row in rows
        if row.asin.strip()
    ]
    if normalized:
        latest = max((m for _, m, _ in normalized), key=_month_ordinal)
        if latest != study_month:
            raise ValueError(
                f"study_month {study_month} does not match the latest month in input data {latest}"
            )

    # Deterministic de-duplication per (asin, month): prefer rows that carry a launch date.
    normalized.sort(key=lambda t: (t[0], _month_ordinal(t[1]), t[2].launch_date is None,
                                   str(t[2].launch_date or ""), t[2].parent_asin))
    kept: dict[str, dict[str, NewProductSnapshot]] = defaultdict(dict)
    exclusions: list[QueueExclusion] = []
    for asin, month, row in normalized:
        if month in kept[asin]:
            exclusions.append(QueueExclusion(asin, month, "duplicate", "same ASIN and month appeared more than once"))
        else:
            kept[asin][month] = row

    first_observed = {
        asin: min(by_month, key=_month_ordinal) for asin, by_month in kept.items()
    }
    crawl_months = sorted({m for by_month in kept.values() for m in by_month}, key=_month_ordinal)
    parent_of = {
        asin: next((r.parent_asin.strip().upper() for _, r in sorted(
            by_month.items(), key=lambda kv: _month_ordinal(kv[0])) if r.parent_asin.strip()), "")
        for asin, by_month in kept.items()
    }
    family_members: dict[str, list[str]] = defaultdict(list)
    for asin, parent in parent_of.items():
        if parent:
            family_members[parent].append(asin)

    def is_old_parent_variant(asin: str) -> bool:
        parent = parent_of[asin]
        if not parent:
            return False
        others = [a for a in family_members[parent] if a != asin]
        if parent in first_observed and parent != asin:
            others.append(parent)
        return any(
            _month_ordinal(first_observed[a]) < _month_ordinal(first_observed[asin]) for a in others
        )

    items: list[NewProductQueueItem] = []
    for asin in sorted(kept):
        by_month = kept[asin]
        ordered = sorted(by_month, key=_month_ordinal)
        launch = next(
            (d for d in (_parse_launch_date(by_month[m].launch_date) for m in ordered) if d), None
        )
        if launch is None:
            exclusions.append(QueueExclusion(
                asin, ordered[0], "no_launch_date", "launch_date missing or not YYYY-MM-DD"
            ))
            continue
        new_months = tuple(
            m for m in ordered if 0 <= (_month_end(m) - launch).days <= window_days
        )
        if not new_months:
            continue  # not a new product in any crawl month

        old_parent = is_old_parent_variant(asin)
        if asin in manual:
            decision = manual[asin]
        elif any(not r.eligible for r in by_month.values()):
            decision = False  # snapshot eligible=False is a manual exclusion
        else:
            decision = None
        if decision is False:
            exclusions.append(QueueExclusion(asin, new_months[0], "manual_ineligible", "manual eligible=False"))
        elif decision is None and old_parent:
            exclusions.append(QueueExclusion(
                asin, new_months[0], "old_parent",
                f"parent_asin {parent_of[asin]} already existed before {first_observed[asin]}",
            ))
        else:
            items.append(NewProductQueueItem(
                asin=asin,
                parent_asin=parent_of[asin],
                launch_date=launch.isoformat(),
                launch_month=f"{launch.year:04d}-{launch.month:02d}",
                first_new_month=new_months[0],
                new_months=new_months,
                old_parent_variant=old_parent,
                manually_included=decision is True,
            ))

    exclusions.sort(key=lambda e: (e.asin, _month_ordinal(e.month), e.reason))
    coverage = _build_coverage(crawl_months, items, study_month, window_days)
    return NewProductQueue(
        study_month=study_month,
        window_days=window_days,
        crawl_months=crawl_months,
        items=items,
        exclusions=exclusions,
        coverage=coverage,
        coverage_gaps=[c.month for c in coverage if not c.covered],
    )


def _build_coverage(
    crawl_months: list[str],
    items: list[NewProductQueueItem],
    study_month: str,
    window_days: int,
) -> list[MonthCoverage]:
    if not crawl_months:
        return []
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item.launch_month] += 1
    first = min([crawl_months[0], *counts], key=_month_ordinal)
    coverage: list[MonthCoverage] = []
    for ordinal in range(_month_ordinal(first), _month_ordinal(study_month) + 1):
        month = _ordinal_to_month(ordinal)
        start, end = _month_start(month), _month_end(month)
        covering = next(
            (
                c for c in crawl_months
                if _month_end(c) >= end and (_month_end(c) - start).days <= window_days
            ),
            None,
        )
        coverage.append(MonthCoverage(
            month=month,
            quarter=_quarter(month),
            covered=covering is not None,
            covering_crawl_month=covering,
            crosses_quarter_boundary=covering is not None and _quarter(covering) != _quarter(month),
            new_product_count=counts.get(month, 0),
        ))
    return coverage
