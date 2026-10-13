from __future__ import annotations

import json

from typing import Any, Iterable
from research_workflow.data.bsr_pipeline import validate_month

from .base import ConnectorError, ConnectorResponse, ToolGateway, with_retries


class SellerSpriteConnector:
    """Thin adapter around JieXi-11/SellerSprite MCP.

    Business modules call semantic methods here; they do not depend on MCP tool
    names or SellerSprite's raw response shape.
    """

    SERVER = "sellersprite"
    SESSION = "sellersprite_session_status"
    LOGIN = "sellersprite_login"
    LIST_OPS = "sellersprite_list_operations"
    DESCRIBE = "sellersprite_describe_operation"
    CALL = "sellersprite_call"

    def __init__(self, gateway: ToolGateway, *, retry_attempts: int = 3) -> None:
        self.gateway = gateway
        self.retry_attempts = retry_attempts

    async def validate(self) -> list[str]:
        tools = await with_retries(lambda: self.gateway.list_tools(self.SERVER))
        required = {self.SESSION, self.LIST_OPS, self.DESCRIBE, self.CALL}
        missing = sorted(required.difference(tools))
        if missing:
            raise ConnectorError(f"SellerSprite MCP missing required tools: {missing}")
        return tools

    async def session_status(self) -> Any:
        return _structured(await with_retries(lambda: self.gateway.call_tool(self.SERVER, self.SESSION, {})))

    async def login(self, wait_seconds: int = 180) -> Any:
        if not 0 <= wait_seconds <= 300:
            raise ValueError("wait_seconds must be between 0 and 300")
        return _structured(await with_retries(
            lambda: self.gateway.call_tool(self.SERVER, self.LOGIN, {"waitSeconds": int(wait_seconds)}),
            attempts=1, timeout_seconds=wait_seconds + 30))

    async def find_operations(self, query: str) -> Any:
        return await self.gateway.call_tool(self.SERVER, self.LIST_OPS, {"query": query})

    async def describe_operation(self, operation: str) -> Any:
        return await self.gateway.call_tool(
            self.SERVER, self.DESCRIBE, {"operation": operation}
        )

    async def call_operation(self, operation: str, params: dict[str, Any]) -> ConnectorResponse:
        async def _call() -> Any:
            return await self.gateway.call_tool(
                self.SERVER,
                self.CALL,
                {"operation": operation, "params": params},
            )

        raw = await with_retries(_call, attempts=self.retry_attempts)
        return ConnectorResponse(
            provider=self.SERVER,
            operation=operation,
            data=_supplier_payload(_structured(raw)),
            raw=raw,
        )

    async def product_research(
        self,
        *,
        marketplace: str,
        keyword: str | None = None,
        month: str | None = None,
        node_id_paths: Iterable[str] | None = None,
        page_size: int = 100,
        max_pages: int = 20,
        extra: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> ConnectorResponse:
        """Fetch product research rows using verified SellerSprite operation.

        SellerSprite currently caps size at 100 and overall results around 2000,
        therefore pagination remains explicit and bounded here.

        ``limit`` requests only the top-``limit`` rows of the sales-descending
        result: fetching stops (and counts as complete) once ``limit`` rows are
        collected, and rows are truncated to ``limit``. Without it, the whole
        result set must be fetched or the call raises.
        """
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        if month:
            validate_month(month)
        rows: list[dict[str, Any]] = []
        raw_pages: list[Any] = []
        complete = False
        previous_page = None
        seen_asins: set[str] = set()
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "marketplace": marketplace.upper(),
                "page": page,
                "size": page_size,
                "order": {"field": "amz_unit", "desc": True},
            }
            if keyword:
                params["keyword"] = keyword
            if month:
                params["month"] = month.replace("-", "")[:6]
            if node_id_paths:
                params["nodeIdPaths"] = list(node_id_paths)
            if extra and set(extra) & set(params):
                raise ValueError("extra cannot override pagination or request identity")
            params.update(extra or {})
            response = await self.call_operation("product_research", params)
            raw_pages.append(response.data)
            page_rows, total = _extract_rows_and_total(response.data)
            if page_rows and page_rows == previous_page:
                raise ConnectorError("Repeated pagination page; results are incomplete")
            previous_page = page_rows
            for row in page_rows:
                asin = str(row.get("asin") or row.get("ASIN") or "").strip().upper()
                if asin and asin in seen_asins:
                    raise ConnectorError("Overlapping pagination ASINs; results are incomplete")
                if asin:
                    seen_asins.add(asin)
            rows.extend(page_rows)
            if limit is not None and len(rows) >= limit:
                rows = rows[:limit]
                complete = True
                break
            if total is not None and len(rows) >= total:
                complete = True
                break
            if len(page_rows) < page_size:
                complete = total is None
                break
        if not complete:
            raise ConnectorError("Incomplete pagination; narrow filters or raise max_pages within the limit")
        return ConnectorResponse(
            provider=self.SERVER,
            operation="product_research",
            data={"items": rows, "count": len(rows)},
            raw=raw_pages,
        )

    async def product_research_by_nodes(
        self,
        *,
        marketplace: str,
        keyword: str | None = None,
        month: str | None = None,
        node_id_paths_list: Iterable[str],
        page_size: int = 100,
        max_pages: int = 20,
        extra: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, ConnectorResponse]:
        """Fetch each node separately (node x month) so results are never mixed.

        Every node gets its own bounded-pagination ``product_research`` call; the
        caller stores per-node responses and merges them downstream. A failing
        node raises instead of returning a partial dict.
        """
        nodes = list(dict.fromkeys(node_id_paths_list))
        if not nodes:
            raise ValueError("node_id_paths_list must not be empty")
        responses: dict[str, ConnectorResponse] = {}
        for node in nodes:
            responses[node] = await self.product_research(
                marketplace=marketplace,
                keyword=keyword,
                month=month,
                node_id_paths=[node],
                page_size=page_size,
                max_pages=max_pages,
                extra=extra,
                limit=limit,
            )
        return responses

    async def asin_detail(self, *, marketplace: str, asin: str) -> ConnectorResponse:
        return await self.call_operation(
            "asin_detail",
            {"marketplace": marketplace.upper(), "asin": asin.strip().upper()},
        )

    async def competitor_lookup(
        self, *, marketplace: str, asins: Iterable[str], month: str | None = None
    ) -> ConnectorResponse:
        values = [a.strip().upper() for a in asins if a.strip()]
        if not values:
            return ConnectorResponse(self.SERVER, "competitor_lookup", {"items": []}, [])
        all_rows: list[dict[str, Any]] = []
        raw_batches: list[Any] = []
        for start in range(0, len(values), 40):
            params: dict[str, Any] = {
                "marketplace": marketplace.upper(),
                "asins": values[start : start + 40],
                "page": 1,
                "size": 100,
            }
            if month:
                params["month"] = month.replace("-", "")[:6]
            response = await self.call_operation("competitor_lookup", params)
            raw_batches.append(response.raw)
            rows, _ = _extract_rows_and_total(response.data)
            all_rows.extend(rows)
        return ConnectorResponse(
            self.SERVER,
            "competitor_lookup",
            {"items": all_rows, "count": len(all_rows)},
            raw_batches,
        )

    async def reviews(
        self,
        *,
        marketplace: str,
        asin: str,
        max_pages: int = 50,
        stars: Iterable[int] | None = None,
    ) -> ConnectorResponse:
        # SellerSprite review operation currently caps size at 10.
        rows: list[dict[str, Any]] = []
        raw_pages: list[Any] = []
        for page in range(1, max_pages + 1):
            response = await self.call_operation(
                "review",
                {
                    "marketplace": marketplace.upper(),
                    "asin": asin.strip().upper(),
                    "starList": list(stars or []),
                    "typeList": [],
                    "page": page,
                    "size": 10,
                },
            )
            raw_pages.append(response.data)
            page_rows, total = _extract_rows_and_total(response.data)
            rows.extend(page_rows)
            if len(page_rows) < 10:
                break
            if total is not None and len(rows) >= total:
                break
        return ConnectorResponse(
            self.SERVER,
            "review",
            {"items": rows, "count": len(rows)},
            raw_pages,
        )


def _structured(result: Any) -> Any:
    """Accept SDK objects and upstream text-only JSON; reject tool failures."""
    if result is None:
        raise ConnectorError("Empty MCP response")
    def get(key, default=None):
        return result.get(key, default) if isinstance(result, dict) else getattr(result, key, default)
    if get("isError", False) or get("is_error", False):
        raise ConnectorError("MCP tool reported an error; check session and operation schema")
    for key in ("structured_content", "structuredContent"):
        value = get(key)
        if value is not None:
            return value
    content = get("content")
    if content is not None:
        text = "\n".join((c.get("text", "") if isinstance(c, dict) else getattr(c, "text", ""))
                         for c in content)
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("MCP response did not contain valid JSON") from exc
    return result


def _supplier_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    status = value.get("status")
    if isinstance(status, int) and status >= 400:
        raise ConnectorError("Supplier HTTP request failed; verify account and request scope")
    if value.get("error") or value.get("success") is False:
        raise ConnectorError("Supplier reported an error")
    code = value.get("code")
    if code is not None and code not in ("OK", "SUCCESS", "0", 0, 200, "200"):
        raise ConnectorError("Supplier business request failed; verify account and schema")
    if "result" in value and "status" in value:
        return _supplier_payload(value["result"])
    return value


def _extract_rows_and_total(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Handle common MCP/SellerSprite envelope variants without leaking them upstream."""
    data = _supplier_payload(data)
    if data is None:
        raise ConnectorError("Missing product response")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], None
    if not isinstance(data, dict):
        return [], None

    candidates: list[Any] = [data]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)], None
        if not isinstance(candidate, dict):
            raise ConnectorError("Invalid product response envelope")
        if len(candidates) > 20:
            raise ConnectorError("Product response envelope is too deeply nested")
        for key in ("data", "result", "payload"):
            value = candidate.get(key)
            if isinstance(value, dict):
                candidates.append(_supplier_payload(value))
            elif isinstance(value, list):
                return [x for x in value if isinstance(x, dict)], _int_or_none(candidate.get("total"))

    for candidate in candidates:
        for key in ("items", "list", "rows", "records", "content"):
            value = candidate.get(key) if isinstance(candidate, dict) else None
            if isinstance(value, list):
                total = None
                for total_key in ("total", "count", "totalElements", "totalCount"):
                    if isinstance(candidate, dict) and total_key in candidate:
                        total = _int_or_none(candidate.get(total_key))
                        break
                    if total_key in data:
                        total = _int_or_none(data.get(total_key))
                        break
                return [x for x in value if isinstance(x, dict)], total
    # A single ASIN detail is itself a record.
    if any(k in data for k in ("asin", "title", "brand")):
        return [data], 1
    raise ConnectorError("Unrecognized product response schema; inspect supplier schema before importing")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
