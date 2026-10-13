from __future__ import annotations

import asyncio
import logging
import re
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

logger = logging.getLogger(__name__)

_MAX_LOG_MESSAGE_CHARS = 200
# 日志里的异常消息先脱敏再截断:Bearer/令牌、key=value 形式的凭据、长随机串。
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|cookie)\b\s*[=:]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"[A-Za-z0-9_\-]{32,}"),
)


def safe_exception_summary(exc: BaseException) -> str:
    """``类型名: 脱敏并截断后的消息``,供日志使用;绝不写完整异常对象或响应体。"""
    message = " ".join(str(exc).split())
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    if len(message) > _MAX_LOG_MESSAGE_CHARS:
        message = message[:_MAX_LOG_MESSAGE_CHARS] + "...(truncated)"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.25,
    timeout_seconds: float = 120.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    operation: str = "connector call",
) -> T:
    """Small retry primitive used by connector adapters.

    It deliberately has no dependency on tenacity/backoff so the research core
    remains easy to run. Production callers may configure a non-zero delay.

    Each retry and the final failure are logged at WARNING with ``operation`` and the
    redacted/truncated exception summary; the raised ``ConnectorError`` stays desensitised
    (exception type name only).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return await asyncio.wait_for(call(), timeout=timeout_seconds)
        except ConnectorError:
            raise
        except retry_exceptions as exc:  # type: ignore[misc]
            last = exc
            if index + 1 >= attempts:
                logger.warning(
                    "%s failed after %d attempts: %s", operation, attempts, safe_exception_summary(exc)
                )
                break
            logger.warning(
                "%s attempt %d/%d failed, retrying: %s",
                operation, index + 1, attempts, safe_exception_summary(exc),
            )
            if base_delay_seconds > 0:
                await asyncio.sleep(base_delay_seconds * (2**index))
    raise ConnectorError(f"connector call failed after {attempts} attempts ({type(last).__name__})") from last
