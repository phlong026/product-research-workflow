from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: str) -> str:
    return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class MCPAuthConfig:
    header: str
    scheme: str
    token_env: str

    def build_headers(self) -> dict[str, str]:
        token = os.getenv(self.token_env, "").strip()
        if not token:
            return {}
        value = f"{self.scheme} {token}".strip() if self.scheme else token
        return {self.header: value}


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    auth: MCPAuthConfig | None = None

    @property
    def configured(self) -> bool:
        if self.transport == "stdio":
            return bool(self.command)
        if self.transport == "http":
            return bool(self.url)
        return False

    def headers(self) -> dict[str, str]:
        return self.auth.build_headers() if self.auth else {}


class MCPRegistry:
    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers

    def get(self, name: str) -> MCPServerConfig:
        try:
            return self._servers[name]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP server: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._servers))

    @classmethod
    def load(cls, path: str | Path) -> "MCPRegistry":
        payload = _expand(json.loads(Path(path).read_text(encoding="utf-8")))
        servers: dict[str, MCPServerConfig] = {}
        for name, raw in payload.get("servers", {}).items():
            auth_raw = raw.get("auth")
            auth = None
            if auth_raw:
                auth = MCPAuthConfig(
                    header=auth_raw["header"],
                    scheme=auth_raw.get("scheme", ""),
                    token_env=auth_raw["token_env"],
                )
            servers[name] = MCPServerConfig(
                name=name,
                transport=raw["transport"],
                url=raw.get("url") or None,
                command=raw.get("command") or None,
                args=tuple(raw.get("args", [])),
                env={key: str(value) for key, value in raw.get("env", {}).items()},
                auth=auth,
            )
        return cls(servers)
