"""ingest --live 的节点参数、月份窗口自动化与节点发现命令(T1-1/T1-2/T1-3 接线)。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from scripts.run_research import main

TOOLS = [
    "sellersprite_session_status",
    "sellersprite_login",
    "sellersprite_list_operations",
    "sellersprite_describe_operation",
    "sellersprite_call",
]


def _row(asin: str, sales: int, node: str, rank: int = 1, parent: str = "") -> dict:
    return {"asin": asin, "monthly_sales": sales, "brand": f"B{asin}", "title": f"Ice Maker {asin}",
            "rank": rank, "seller_region": "US", "parentAsin": parent, "nodeIdPath": node,
            "nodeLabelPath": f"Home>{node}"}


# (month YYYYMM, node or None) -> rows;未登记的组合返回空 = 该月无数据
DATA = {
    ("202601", "n1"): [_row("A1", 90, "n1", 1), _row("A2", 50, "n1", 2)],
    ("202601", "n2"): [_row("A2", 50, "n2", 3), _row("A3", 30, "n2", 4)],
    ("202512", "n1"): [_row("A1", 80, "n1", 1), _row("A2", 40, "n1", 2)],
    ("202512", "n2"): [_row("A3", 35, "n2", 1)],
    ("202601", None): [_row("A1", 90, "n1", 1), _row("A2", 50, "n1", 2), _row("A3", 30, "n2", 4)],
    ("202512", None): [_row("A1", 80, "n1", 1)],
}


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list_tools(self, server_name: str) -> list[str]:
        return sorted(TOOLS)

    async def call_tool(self, server_name, tool_name, arguments):
        if tool_name == "sellersprite_session_status":
            return {"structured_content": {"loggedIn": True}}
        params = arguments["params"]
        self.calls.append(params)
        nodes = params.get("nodeIdPaths")
        rows = DATA.get((params["month"], nodes[0] if nodes else None), [])
        return {"structured_content": {"items": rows, "total": len(rows)}}


@pytest.fixture()
def gateway(monkeypatch):
    import scripts.run_research as rr

    gw = FakeGateway()
    monkeypatch.setattr(rr, "_build_gateway", lambda config_path: gw)
    monkeypatch.setattr(rr, "_today", lambda: date(2026, 2, 15))
    return gw


def _runner(tmp_path, capsys):
    def run(*argv: str) -> dict:
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    return run


def test_discover_nodes_lists_candidates_and_requires_confirmation(gateway, tmp_path, capsys) -> None:
    run = _runner(tmp_path, capsys)
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = run("discover-nodes", "--project", pid, "--keyword", "ice maker", "--month", "2026-01")
    assert [c["node_id"] for c in out["candidates"]][0] == "n1"
    assert out["candidates"][0]["asin_count"] == 2
    assert out["main_node_suggestion"]["node_id"] == "n1"
    assert out["needs_human_confirmation"] is True
    saved = json.loads((tmp_path / "projects" / pid / "nodes.json").read_text(encoding="utf-8"))
    assert saved["needs_human_confirmation"] is True


def test_live_ingest_with_nodes_merges_by_asin_and_stores_per_node_month(gateway, tmp_path, capsys) -> None:
    run = _runner(tmp_path, capsys)
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = run("ingest", "--project", pid, "--live", "--keyword", "ice maker",
              "--months", "2025-12,2026-01", "--nodes", "n1,n2")
    pdir = tmp_path / "projects" / pid
    records = json.loads((pdir / "records.json").read_text(encoding="utf-8"))
    assert sorted(r["asin"] for r in records["2026-01"]) == ["A1", "A2", "A3"]  # A2 跨节点只留一条
    assert (pdir / "raw" / "2026-01__n1.json").exists() and (pdir / "raw" / "2026-01__n2.json").exists()
    sources = json.loads((pdir / "sources.json").read_text(encoding="utf-8"))
    assert {"2026-01:sellersprite:n1", "2026-01:sellersprite:n2"} <= set(sources)
    assert out["nodes"] == ["n1", "n2"]


def test_auto_months_reports_missing_months_but_keeps_available(gateway, tmp_path, capsys) -> None:
    run = _runner(tmp_path, capsys)
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = run("ingest", "--project", pid, "--live", "--keyword", "ice maker", "--months", "auto")
    # today=2026-02-15, lag=1 → 窗口末月 2026-01;36 个月中仅 2025-12 与 2026-01 有数据
    assert set(out["months"]) == {"2025-12", "2026-01"}
    assert len(out["missing_months"]) == 34
    assert out["coverage"]["complete"] is False
    coverage = json.loads((tmp_path / "projects" / pid / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["missing"] == sorted(out["missing_months"])


def test_explicit_months_with_missing_month_is_an_error(gateway, tmp_path, capsys) -> None:
    run = _runner(tmp_path, capsys)
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    with pytest.raises(SystemExit):
        run("ingest", "--project", pid, "--live", "--keyword", "ice maker", "--months", "2025-11,2026-01")
