"""新品筛选：近 N 天上架，且排除“老商品带出来的新变体”。

规则（均可在人工复核表里看到原因）：
- 上架时间缺失/无法解析 → 不纳入，原因 launch_date_unknown（不猜测）
- 上架时间不在 (截止日-N天, 截止日] → 不纳入，原因 not_in_window / after_cutoff
- 同父体下存在上架时间早于窗口起点的其他 ASIN → 视为老商品新增变体，不纳入，原因 variant_of_old_parent
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Iterable

from research_workflow.data.models import BSRRecord


@dataclass(slots=True, frozen=True)
class NewProductCandidate:
    asin: str
    parent_asin: str
    launch_date: str | None
    eligible: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_launch_date(value: str | None) -> date | None:
    text = (value or "").strip()[:10]
    for size, builder in ((10, lambda t: date(int(t[:4]), int(t[5:7]), int(t[8:10]))),
                          (7, lambda t: date(int(t[:4]), int(t[5:7]), 1))):
        if len(text) >= size and text[4] == "-":
            try:
                return builder(text[:size])
            except ValueError:
                return None
    return None


def month_end(month: str) -> date:
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, calendar.monthrange(year, mon)[1])


def select_new_products(
    records: Iterable[BSRRecord], as_of_month: str, window_days: int = 90
) -> list[NewProductCandidate]:
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    cutoff = month_end(as_of_month)
    window_start = cutoff - timedelta(days=window_days)
    launch_by_asin: dict[str, str | None] = {}
    parent_by_asin: dict[str, str] = {}
    for r in records:
        if r.launch_date and not launch_by_asin.get(r.asin):
            launch_by_asin[r.asin] = r.launch_date
        launch_by_asin.setdefault(r.asin, r.launch_date)
        parent_by_asin.setdefault(r.asin, r.parent_asin)
    old_parents = {
        parent_by_asin[a] for a, raw in launch_by_asin.items()
        if parent_by_asin[a] and (d := parse_launch_date(raw)) is not None and d <= window_start
    }
    out: list[NewProductCandidate] = []
    for asin in sorted(launch_by_asin):
        raw = launch_by_asin[asin]
        parent = parent_by_asin[asin]
        launched = parse_launch_date(raw)
        if launched is None:
            eligible, reason = False, "launch_date_unknown"
        elif launched > cutoff:
            eligible, reason = False, "after_cutoff"
        elif launched <= window_start:
            eligible, reason = False, "not_in_window"
        elif parent and parent in old_parents:
            eligible, reason = False, "variant_of_old_parent"
        else:
            eligible, reason = True, "ok"
        out.append(NewProductCandidate(asin, parent, raw, eligible, reason))
    return out
