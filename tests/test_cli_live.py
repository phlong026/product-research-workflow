"""ingest --live(SellerSprite MCP 直连)的假网关集成测试。

不依赖真实 MCP/密钥:替换 _build_gateway 注入 FakeGateway,验证
直连取数 → 清洗 → 数据检查点 → raw 留档 的完整链路。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_research import main

REQUIRED_TOOLS = {
    "sellersprite_session_status",
    "sellersprite_login",
    "sellersprite_list_operations",
    "sellersprite_describe_operation",
    "sellersprite_call",
}

PAGES = {
    "202501": [
        {"asin": "N1", "monthly_sales": 20, "brand": "NBrand", "title": "Nugget Ice Maker", "rank": 1, "seller_region": "CN"},
        {"asin": "B1", "monthly_sales": 80, "brand": "BBrand", "title": "Bullet Ice Maker", "rank": 2, "seller_region": "US"},
    ],
    "202601": [
        {"asin": "N1", "monthly_sales": 25, "brand": "NBrand", "title": "Nugget Ice Maker", "rank": 1, "seller_region": "CN"},
        {"asin": "B1", "monthly_sales": 55, "brand": "BBrand", "title": "Bullet Ice Maker", "rank": 2, "seller_region": "US"},
    ],
}


class FakeGateway:
    async def list_tools(self, server_name: str) -> list[str]:
        assert server_name == "sellersprite"
        return sorted(REQUIRED_TOOLS)

    async def call_tool(self, server_name, tool_name, arguments):
        if tool_name == "sellersprite_session_status":
            return {"structured_content": {"loggedIn": True}}
        assert tool_name == "sellersprite_call"
        assert arguments["operation"] == "product_research"
        month = arguments["params"]["month"]  # YYYYMM
        rows = PAGES[month]
        return {"structured_content": {"items": rows, "total": len(rows)}}


@pytest.fixture()
def fake_gateway(monkeypatch):
    import scripts.run_research as rr

    monkeypatch.setattr(rr, "_build_gateway", lambda config_path: FakeGateway())


def test_live_ingest_end_to_end(fake_gateway, tmp_path, capsys) -> None:
    def run(*argv: str) -> dict:
        capsys.readouterr()
        main(
            [
                "--db", str(tmp_path / "db.sqlite"),
                "--root", str(tmp_path / "projects"),
                *argv,
            ]
        )
        return json.loads(capsys.readouterr().out)

    out = run("init", "--product", "Ice Maker", "--marketplace", "US")
    project_id = out["project_id"]

    out = run(
        "ingest",
        "--project", project_id,
        "--live",
        "--keyword", "ice maker",
        "--months", "2025-01,2026-01",
    )
    assert out["source_mode"] == "mcp:sellersprite"
    assert set(out["months"]) == {"2025-01", "2026-01"}
    assert out["months"]["2025-01"]["output_rows"] == 2
    assert out["checkpoint_step"] == "data_review"

    pdir = tmp_path / "projects" / project_id
    records = json.loads((pdir / "records.json").read_text(encoding="utf-8"))
    assert {row["asin"] for row in records["2025-01"]} == {"N1", "B1"}
    assert records["2025-01"][0]["asin"] == "B1"  # 清洗后按销量降序
    # 原始响应留档(证据链 + 字段映射排查用)
    raw = json.loads((pdir / "raw" / "2025-01.json").read_text(encoding="utf-8"))
    assert raw["provider"] == "sellersprite" and len(raw["rows"]) == 2
    sources = json.loads((pdir / "sources.json").read_text(encoding="utf-8"))
    assert "2025-01:sellersprite" in sources and sources["2025-01:sellersprite"]["kind"] == "mcp"

    out = run("resolve", "--project", project_id, "--checkpoint", out["checkpoint_id"], "--approve")
    assert out["next"] == "classify"

    # live 模式缺 --months 时给出可操作的错误
    with pytest.raises(SystemExit):
        run("ingest", "--project", project_id, "--live")
