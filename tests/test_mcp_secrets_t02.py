from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_examples_only_contain_placeholders_not_tokens():
    text = (ROOT / "config/mcp.example.json").read_text(encoding="utf-8") + (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "mcp_80c" not in text
    assert "sifmcp260" not in text
    assert "vte0nh" not in text
    # obvious private-token shapes should be absent from examples
    assert re.search(r"APIFY_TOKEN=\s*$", text, re.MULTILINE)
    assert re.search(r"XIYOU_MCP_TOKEN=\s*$", text, re.MULTILINE)
