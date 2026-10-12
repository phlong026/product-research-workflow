from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeVar


@dataclass(slots=True, frozen=True)
class ConnectorResponse:
    provider: str
    operation: str
    data: Any
    raw: Any | None = None


class ToolGateway(Protocol):
    async def list_tools(self, server_name: str) -> list[str]: ...

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any: ...


class ConnectorError(RuntimeError):
    pass


T = TypeVar("T")


async def with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Small retry primitive used by connector adapters.

    It deliberately has no dependency on tenacity/backoff so the research core
    remains easy to run. Production callers may configure a non-zero delay.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return await call()
        except retry_exceptions as exc:  # type: ignore[misc]
            last = exc
            if index + 1 >= attempts:
                break
            if base_delay_seconds > 0:
                await asyncio.sleep(base_delay_seconds * (2**index))
    raise ConnectorError(f"connector call failed after {attempts} attempts: {last}") from last
