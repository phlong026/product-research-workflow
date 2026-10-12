from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .config import MCPRegistry, MCPServerConfig


class MCPGateway:
    """Thin adapter over the official MCP Python SDK v2.

    Imports stay lazy so all offline pipeline and Golden Case tests can run
    without MCP runtime dependencies or credentials.
    """

    def __init__(self, registry: MCPRegistry) -> None:
        self.registry = registry

    @asynccontextmanager
    async def _client(self, config: MCPServerConfig) -> AsyncIterator[Any]:
        try:
            from mcp import Client, StdioServerParameters
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError("Install MCP dependency: pip install -e .") from exc

        if config.transport == "stdio":
            params = StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                env=config.env or None,
            )
            async with Client(params) as client:
                yield client
            return

        if config.transport != "http" or not config.url:
            raise ValueError(f"Unsupported MCP config for {config.name}: {config.transport}")

        headers = config.headers()
        if not headers:
            async with Client(config.url) as client:
                yield client
            return

        try:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError("Install MCP HTTP dependencies (mcp v2 includes httpx2)") from exc

        async with httpx2.AsyncClient(headers=headers) as http_client:
            transport = streamable_http_client(config.url, http_client=http_client)
            async with Client(transport) as client:
                yield client

    async def list_tools(self, server_name: str) -> list[str]:
        config = self.registry.get(server_name)
        async with self._client(config) as client:
            names: list[str] = []
            cursor = None
            while True:
                result = await client.list_tools(cursor=cursor)
                names.extend(tool.name for tool in result.tools)
                cursor = getattr(result, "next_cursor", None)
                if not cursor:
                    return names

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        config = self.registry.get(server_name)
        async with self._client(config) as client:
            return await client.call_tool(tool_name, arguments or {})
