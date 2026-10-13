from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from research_workflow.data.models import BSRRecord

from .models import PointRankChange, PointRankMatrix, PointRankRow

DEFAULT_POINT_RANKS = (1, 2, 3, 4, 5, 10, 20, 30, 50, 70, 100)


def _month(value: str) -> str:
    year, mon = value[:7].split("-")
    return f"{int(year):04d}-{int(mon):02d}"


def _prior_year(month: str) -> str:
    year, mon = month.split("-")
    return f"{int(year) - 1:04d}-{mon}"


def _sales(record: BSRRecord) -> int:
    return max(0, record.monthly_sales)


def _ordered(rows: list[BSRRecord]) -> tuple[list[BSRRecord], str]:
    """Order one month's records for single-rank lookup.

    sales_rank first (ascending); records without it follow, by sales desc.
    node_rank (source BSR rank) is deliberately not used: point rank is a sales rank.
    """
    ranked = sum(1 for r in rows if r.sales_rank is not None)
    if ranked == 0:
        basis = "monthly_sales_desc"
    elif ranked == len(rows):
        basis = "sales_rank"
    else:
        basis = "mixed"
    ordered = sorted(
        rows,
        key=lambda r: (
            r.sales_rank is None,
            r.sales_rank if r.sales_rank is not None else 0,
            -_sales(r),
            r.asin,
        ),
    )
    return ordered, basis


def build_point_rank_matrix(
    records: Iterable[BSRRecord],
    *,
    ranks: tuple[int, ...] = DEFAULT_POINT_RANKS,
) -> PointRankMatrix:
    """Month x rank matrix of SINGLE-rank sales (point basis, not cumulative).

    The Nth rank is the Nth record of the month's order (see ``_ordered``). A rank
    beyond the number of ASINs that month is ``None`` -- never padded with 0.
    """
    if any(r < 1 for r in ranks):
        raise ValueError("ranks must be positive integers")
    wanted = sorted(set(ranks))

    grouped: dict[str, list[BSRRecord]] = defaultdict(list)
    for record in records:
        grouped[_month(record.month)].append(record)

    rows: list[PointRankRow] = []
    for month in sorted(grouped):
        ordered, basis = _ordered(grouped[month])
        rows.append(
            PointRankRow(
                month=month,
                ordering_basis=basis,
                point_sales={
                    str(rank): _sales(ordered[rank - 1]) if rank <= len(ordered) else None
                    for rank in wanted
                },
                point_asin={
                    str(rank): ordered[rank - 1].asin if rank <= len(ordered) else None
                    for rank in wanted
                },
            )
        )
    return PointRankMatrix(ranks=wanted, rows=rows)


def compare_point_rank_matrices(
    matrix: PointRankMatrix,
    pairs: Iterable[tuple[str, str]] | None = None,
) -> list[PointRankChange]:
    """Per-rank change between two months of the SAME point basis.

    ``pairs`` are (base_month, current_month); default = every month that has its
    prior-year month in the matrix (YoY). Pairs with a missing month are skipped,
    so a coverage gap never turns into an invented change. Output is factual only.
    """
    by_month = {row.month: row for row in matrix.rows}
    if pairs is None:
        pairs = [(_prior_year(m), m) for m in sorted(by_month) if _prior_year(m) in by_month]

    changes: list[PointRankChange] = []
    for base_month, current_month in pairs:
        base_row = by_month.get(_month(base_month))
        current_row = by_month.get(_month(current_month))
        if base_row is None or current_row is None:
            continue
        for rank in matrix.ranks:
            base = base_row.point_sales.get(str(rank))
            current = current_row.point_sales.get(str(rank))
            if base is None or current is None:
                abs_change = rel_change = None
                direction = "unavailable"
            else:
                abs_change = current - base
                rel_change = None if base == 0 else (current / base - 1.0) * 100.0
                direction = "up" if abs_change > 0 else "down" if abs_change < 0 else "flat"
            changes.append(
                PointRankChange(
                    base_month=base_row.month,
                    current_month=current_row.month,
                    rank=rank,
                    base_point_sales=base,
                    current_point_sales=current,
                    abs_change=abs_change,
                    rel_change_pct=rel_change,
                    direction=direction,
                )
            )
    return changes
