import json
from pathlib import Path

from research_workflow.mcp import MCPRegistry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/mcp.example.json"
ENV_EXAMPLE = ROOT / ".env.example"


def test_expected_mcp_servers_are_preconfigured(monkeypatch):
    monkeypatch.setenv("SELLERSPRITE_CDP_URL", "http://127.0.0.1:9222")
    registry = MCPRegistry.load(CONFIG)
    assert set(registry.names()) == {"apify", "sellersprite", "sif", "xiyou"}
    assert registry.get("sellersprite").transport == "stdio"


def test_example_config_contains_no_embedded_tokens():
    payload = CONFIG.read_text(encoding="utf-8")
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # Real tokens must be injected later; examples stay blank/placeholders.
    assert "mcp_80" not in payload
    assert "sifmcp" not in payload.lower()
    for line in env_text.splitlines():
        if line.startswith(("SIF_MCP_TOKEN=", "XIYOU_MCP_TOKEN=", "APIFY_TOKEN=", "SELLERSPRITE_MCP_TOKEN=")):
            assert line.endswith("=")
