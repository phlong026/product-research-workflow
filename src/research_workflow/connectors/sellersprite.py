from __future__ import annotations

from typing import Any, Iterable

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
        tools = await self.gateway.list_tools(self.SERVER)
        required = {self.SESSION, self.LIST_OPS, self.DESCRIBE, self.CALL}
        missing = sorted(required.difference(tools))
        if missing:
            raise ConnectorError(f"SellerSprite MCP missing required tools: {missing}")
        return tools

    async def session_status(self) -> Any:
        return await self.gateway.call_tool(self.SERVER, self.SESSION, {})

    async def login(self, wait_seconds: int = 180) -> Any:
        return await self.gateway.call_tool(
            self.SERVER, self.LOGIN, {"waitSeconds": int(wait_seconds)}
        )

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
            data=_structured(raw),
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
    ) -> ConnectorResponse:
        """Fetch product research rows using verified SellerSprite operation.

        SellerSprite currently caps size at 100 and overall results around 2000,
        therefore pagination remains explicit and bounded here.
        """
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        rows: list[dict[str, Any]] = []
        raw_pages: list[Any] = []
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
            params.update(extra or {})
            response = await self.call_operation("product_research", params)
            raw_pages.append(response.raw)
            page_rows, total = _extract_rows_and_total(response.data)
            rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
            if total is not None and len(rows) >= total:
                break
        return ConnectorResponse(
            provider=self.SERVER,
            operation="product_research",
            data={"items": rows, "count": len(rows)},
            raw=raw_pages,
        )

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
            raw_pages.append(response.raw)
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
    if result is None:
        return None
    if isinstance(result, dict):
        return result.get("structured_content", result.get("structuredContent", result))
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    structured = getattr(result, "structuredContent", None)
    return structured if structured is not None else result


def _extract_rows_and_total(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Handle common MCP/SellerSprite envelope variants without leaking them upstream."""
    if data is None:
        return [], None
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], None
    if not isinstance(data, dict):
        return [], None

    candidates: list[Any] = [data]
    for key in ("data", "result", "payload"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            return [x for x in value if isinstance(x, dict)], _int_or_none(data.get("total"))

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
    return [], _int_or_none(data.get("total") or data.get("count"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
