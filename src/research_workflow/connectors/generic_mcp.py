from __future__ import annotations

from typing import Any

from .base import ConnectorResponse, ToolGateway
from .sellersprite import _structured


class GenericMCPConnector:
    def __init__(self, server_name: str, gateway: ToolGateway) -> None:
        self.server_name = server_name
        self.gateway = gateway

    async def tools(self) -> list[str]:
        return await self.gateway.list_tools(self.server_name)

    async def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> ConnectorResponse:
        raw = await self.gateway.call_tool(self.server_name, tool_name, arguments or {})
        return ConnectorResponse(
            provider=self.server_name,
            operation=tool_name,
            data=_structured(raw),
            raw=raw,
        )


class SIFConnector(GenericMCPConnector):
    def __init__(self, gateway: ToolGateway) -> None:
        super().__init__("sif", gateway)


class XiyouConnector(GenericMCPConnector):
    KNOWN_TOOLS = {
        "get_asin_info",
        "get_asin_variations",
        "get_asin_info_trends",
        "get_asin_keywords",
        "get_asin_keywords_monthly",
        "get_asin_keyword_rank_trends",
        "get_asin_keyword_rank_hourly",
        "get_asin_keyword_traffic_trends",
        "get_asin_traffic",
        "get_asin_traffic_trends",
        "get_asin_order_trends",
        "get_asin_bsr_trends",
        "get_asin_ad_change_trends",
        "get_keyword_info",
        "get_keyword_aba_trends",
        "get_keyword_asin_analysis",
        "get_keyword_analysis_monthly",
    }

    def __init__(self, gateway: ToolGateway) -> None:
        super().__init__("xiyou", gateway)


class ApifyConnector(GenericMCPConnector):
    def __init__(self, gateway: ToolGateway) -> None:
        super().__init__("apify", gateway)

    async def search_actors(self, query: str) -> ConnectorResponse:
        return await self.call("search-actors", {"query": query})

    async def call_actor(self, actor: str, actor_input: dict[str, Any], *, wait_secs: int = 30) -> ConnectorResponse:
        return await self.call(
            "call-actor",
            {"actor": actor, "input": actor_input, "waitSecs": min(max(wait_secs, 0), 45)},
        )
