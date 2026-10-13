"""Month-by-month Top-N candidate pool collection (T1-3).

The month window is computed elsewhere; this module only receives ``months``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from ..connectors.base import ConnectorError, safe_exception_summary
from ..data.bsr_pipeline import validate_month
from .models import MonthlyFetchResult
from .service import ProductResearchSource

logger = logging.getLogger(__name__)

# ``amz_unit`` is the verified order field of product_research; the other
# aliases are inferred (字段名为推断,待真实 schema 验证).
SALES_FIELDS: tuple[str, ...] = ("amz_unit", "monthlySales", "monthly_sales", "销量")


def _sales_of(row: Mapping[str, Any]) -> float | None:
    for name in SALES_FIELDS:
        value = row.get(name)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _top_by_sales(rows: list[dict[str, Any]], top_n: int) -> tuple[dict[str, Any], ...]:
    # sorted() is stable: ties and rows lacking sales keep the server order.
    ordered = sorted(
        rows,
        key=lambda r: (_sales_of(r) is None, -(_sales_of(r) or 0.0)),
    )
    return tuple(ordered[:top_n])


async def fetch_monthly_top(
    connector: ProductResearchSource,
    *,
    marketplace: str,
    keyword: str,
    months: Iterable[str],
    node_id_paths: Iterable[str] | None = None,
    top_n: int = 200,
    page_size: int = 100,
    max_pages: int = 20,
) -> MonthlyFetchResult:
    """Fetch the Top-N (by sales, desc) candidate pool for each month.

    A month whose response is empty or whose connector call fails is recorded in
    ``missing_months`` with a reason; it is never skipped silently or treated as
    zero sales. ``node_id_paths`` is passed through as one request scope.
    """
    month_list = list(dict.fromkeys(months))
    if not month_list:
        raise ValueError("months must not be empty")
    for month in month_list:
        validate_month(month)
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    if top_n > page_size * max_pages:
        raise ValueError(
            f"top_n={top_n} exceeds page budget page_size*max_pages={page_size * max_pages}"
        )
    nodes = list(node_id_paths) if node_id_paths else None
    pages_needed = -(-top_n // page_size)  # ceil: do not fetch beyond the target

    rows_by_month: dict[str, tuple[dict[str, Any], ...]] = {}
    raw_by_month: dict[str, Any] = {}
    requests: dict[str, dict[str, Any]] = {}
    missing: dict[str, str] = {}
    for month in month_list:
        requests[month] = {
            "marketplace": marketplace.upper(),
            "keyword": keyword,
            "month": month,
            "node_id_paths": nodes,
            "top_n": top_n,
            "page_size": page_size,
            "max_pages": pages_needed,
        }
        try:
            response = await connector.product_research(
                marketplace=marketplace,
                keyword=keyword,
                month=month,
                node_id_paths=nodes,
                page_size=page_size,
                max_pages=pages_needed,
                limit=top_n,
            )
        except ConnectorError as exc:
            missing[month] = f"connector_error: {exc}"
            logger.warning(
                "monthly fetch missing month %s (connector_error): %s", month, safe_exception_summary(exc)
            )
            continue
        items = response.data.get("items", []) if isinstance(response.data, dict) else []
        if not items:
            missing[month] = "empty_response: no rows returned"
            logger.warning("monthly fetch missing month %s (empty_response): no rows returned", month)
            continue
        rows_by_month[month] = _top_by_sales(items, top_n)
        raw_by_month[month] = response.raw
    return MonthlyFetchResult(
        months=tuple(month_list),
        rows_by_month=rows_by_month,
        raw_by_month=raw_by_month,
        requests=requests,
        missing_months=missing,
    )
