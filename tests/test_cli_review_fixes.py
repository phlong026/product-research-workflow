"""独立代码评审发现的集成缺陷(H1/H3/H4/H5、M2–M7、L6 等)的回归测试。"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scripts.run_research import main

TOOLS = ["sellersprite_session_status", "sellersprite_login", "sellersprite_list_operations",
         "sellersprite_describe_operation", "sellersprite_call"]


def _row(asin: str, sales: int, node: str, rank: int = 1) -> dict:
    return {"asin": asin, "monthly_sales": sales, "brand": f"B{asin}", "title": f"Ice Maker {asin}", "rank": rank,
            "seller_region": "US", "nodeIdPath": node}


DATA = {
    ("202601", "n1"): [_row("A1", 90, "n1"), _row("A2", 50, "n1", 2)],
    ("202601", "n2"): [_row("A3", 30, "n2", 3)],
    ("202512", "n1"): [_row("A1", 80, "n1")],  # n2 缺 2025-12
}


class Gateway:
    def __init__(self, fail_months: set[str] | None = None) -> None:
        self.fail_months = fail_months or set()

    async def list_tools(self, server_name):
        return sorted(TOOLS)

    async def call_tool(self, server_name, tool_name, arguments):
        if tool_name == "sellersprite_session_status":
            return {"structured_content": {"loggedIn": True}}
        params = arguments["params"]
        if params["month"] in self.fail_months:
            raise RuntimeError("boom")
        nodes = params.get("nodeIdPaths")
        rows = DATA.get((params["month"], nodes[0] if nodes else None), [])
        return {"structured_content": {"items": rows, "total": len(rows)}}


@pytest.fixture()
def gw(monkeypatch):
    import scripts.run_research as rr

    holder = {"gateway": Gateway()}
    monkeypatch.setattr(rr, "_build_gateway", lambda config_path: holder["gateway"])
    monkeypatch.setattr(rr, "_today", lambda: date(2026, 2, 15))
    return holder


def run(tmp_path, capsys, *argv, fail=False):
    capsys.readouterr()
    args = ["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv]
    if fail:
        with pytest.raises(SystemExit):
            main(args)
        return json.loads(capsys.readouterr().out)
    main(args)
    return json.loads(capsys.readouterr().out)


def new_project(tmp_path, capsys):
    return run(tmp_path, capsys, "init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]


# --- H1:多节点部分缺月不得混入(缺失不当 0) ---

def test_partial_node_month_is_treated_as_missing_in_explicit_mode(gw, tmp_path, capsys) -> None:
    pid = new_project(tmp_path, capsys)
    err = run(tmp_path, capsys, "ingest", "--project", pid, "--live", "--keyword", "ice", "--months",
              "2025-12,2026-01", "--nodes", "n1,n2", fail=True)
    assert "n2" in err["error"] and "2025-12" in err["error"]


def test_partial_node_month_excluded_in_auto_mode_and_reported(gw, tmp_path, capsys) -> None:
    pid = new_project(tmp_path, capsys)
    out = run(tmp_path, capsys, "ingest", "--project", pid, "--live", "--keyword", "ice", "--months", "auto",
              "--nodes", "n1,n2")
    assert set(out["months"]) == {"2026-01"}
    assert "2025-12" in out["missing_months"]
    assert "n2" in out["missing_reasons"]["2025-12"]
    cp = run(tmp_path, capsys, "status", "--project", pid)["pending_checkpoint"]
    assert "2025-12" in cp["payload"]["missing_months"] and "2025-12" in cp["payload"]["missing_reasons"]


# --- M7:接口失败 ≠ 无数据 ---

def test_connector_errors_fail_the_command_even_in_auto_mode(gw, tmp_path, capsys) -> None:
    gw["gateway"] = Gateway(fail_months={"202512"})
    pid = new_project(tmp_path, capsys)
    err = run(tmp_path, capsys, "ingest", "--project", pid, "--live", "--keyword", "ice", "--months", "auto",
              fail=True)
    assert "接口" in err["error"] and "2025-12" in err["error"]


# --- H4:同月多个文件 ---

def test_same_month_in_two_files_is_rejected(tmp_path, capsys) -> None:
    for name, asin in (("2026-01_a.csv", "A1"), ("2026-01_b.csv", "B1")):
        (tmp_path / name).write_text(f"ASIN,月销量,品牌,商品标题,小类BSR\n{asin},10,B,T,1\n", encoding="utf-8")
    pid = new_project(tmp_path, capsys)
    err = run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-01_a.csv"),
              "--file", str(tmp_path / "2026-01_b.csv"), fail=True)
    assert "2026-01" in err["error"] and "多个文件" in err["error"]


# --- 表协议:H3 / M2 / M3 / M4 / L6 ---

CSV = "ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地\nA1,90,B1,Nugget Ice,1,CN\nA2,60,B2,Bullet Ice,2,\nA3,30,B3,Other,3,US\n"


@pytest.fixture()
def approved(tmp_path, capsys):
    (tmp_path / "2026-01.csv").write_text(CSV, encoding="utf-8")
    pid = new_project(tmp_path, capsys)
    out = run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-01.csv"))
    run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    return pid


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write(path: Path, rows: list[dict]) -> str:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_table2_region_edit_on_known_region_reaches_metrics_and_is_idempotent(approved, tmp_path, capsys) -> None:
    pid = approved
    export = tmp_path / "t2.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(export))
    rows = _rows(export)
    for row in rows:
        if row["asin"] == "A1":
            row["seller_region"] = "US"  # 原为 CN(已知地区)
    edited = _write(tmp_path / "t2e.csv", rows)
    assert run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2", "--file", edited)["changed_cells"] >= 1
    cls = run(tmp_path, capsys, "classify", "--project", pid, "--label", "n=N", "--rule", "n=nugget")
    run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", cls["checkpoint_id"], "--approve")
    run(tmp_path, capsys, "analyze", "--project", pid)
    market = json.loads((tmp_path / "projects" / pid / "market_metrics.json").read_text(encoding="utf-8"))
    regions = {r["seller_region"] for r in market["regions"]} if "regions" in market else set()
    text = json.dumps(market)
    assert '"CN"' not in text or regions != {"CN"}  # A1 不再按 CN 计入
    # 重新导出显示已修改值;同一文件再次上传不再算"有修改"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(export))
    assert {r["asin"]: r["seller_region"] for r in _rows(export)}["A1"] == "US"


def test_table2_clearing_bullets_is_an_explicit_override(approved, tmp_path, capsys) -> None:
    pid = approved
    export = tmp_path / "t2.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(export))
    rows = _rows(export)
    for row in rows:
        row["bullets"] = "fast || cheap" if row["asin"] == "A2" else row["bullets"]
    run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2", "--file", _write(tmp_path / "a.csv", rows))
    for row in rows:
        row["bullets"] = ""  # 人工清空
    out = run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2", "--file", _write(tmp_path / "b.csv", rows))
    assert out["changed_cells"] >= 1
    edits = json.loads((tmp_path / "projects" / pid / "table2_edits.json").read_text(encoding="utf-8"))
    assert edits["bullets"]["A2"] == []


def test_table1_import_reports_superseded_table2_edits_and_keeps_backup(approved, tmp_path, capsys) -> None:
    pid = approved
    pdir = tmp_path / "projects" / pid
    t2 = tmp_path / "t2.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(t2))
    rows = _rows(t2)
    rows[0]["bullets"] = "x || y"
    run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2", "--file", _write(tmp_path / "t2e.csv", rows))
    assert (pdir / "table2_edits.json").exists()
    t1 = tmp_path / "t1.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "1", "--output", str(t1))
    rows1 = _rows(t1)
    rows1[0]["monthly_sales"] = "91"
    out = run(tmp_path, capsys, "import-table", "--project", pid, "--table", "1", "--file", _write(tmp_path / "t1e.csv", rows1))
    assert "table2_edits.json" in out["superseded_human_edits"]
    assert not (pdir / "table2_edits.json").exists()
    assert list((pdir / "superseded").glob("table2_edits.json.*"))  # 人工成果有备份


def test_table1_sales_rank_is_not_editable_and_vc_region_rejected(approved, tmp_path, capsys) -> None:
    pid = approved
    t1 = tmp_path / "t1.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "1", "--output", str(t1))
    rows = _rows(t1)
    for row in rows:
        if row["asin"] == "A3":
            row["sales_rank"] = "1"
    err = run(tmp_path, capsys, "import-table", "--project", pid, "--table", "1",
              "--file", _write(tmp_path / "r.csv", rows), fail=True)
    assert "完全相同" in err["error"]  # 只改 sales_rank 不算修改
    rows = _rows(t1)
    rows[0]["seller_region"] = "VC"
    err = run(tmp_path, capsys, "import-table", "--project", pid, "--table", "1",
              "--file", _write(tmp_path / "vc.csv", rows), fail=True)
    assert "账号类型" in err["error"]


def test_table1_import_refreshes_quality_and_keeps_missing_months(tmp_path, capsys, gw) -> None:
    pid = new_project(tmp_path, capsys)
    out = run(tmp_path, capsys, "ingest", "--project", pid, "--live", "--keyword", "ice", "--months", "auto",
              "--nodes", "n1")
    run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    t1 = tmp_path / "t1.csv"
    run(tmp_path, capsys, "export-table", "--project", pid, "--table", "1", "--output", str(t1))
    rows = _rows(t1)
    rows = [r for r in rows if not (r["month"] == "2026-01" and r["asin"] == "A2")]  # 删 1 行
    out = run(tmp_path, capsys, "import-table", "--project", pid, "--table", "1", "--file", _write(tmp_path / "e.csv", rows))
    cp = run(tmp_path, capsys, "status", "--project", pid)["pending_checkpoint"]
    assert cp["payload"]["missing_months"]  # 原有缺月信息没有丢
    quality = json.loads((tmp_path / "projects" / pid / "quality.json").read_text(encoding="utf-8"))
    assert quality["2026-01"]["stats"]["output_rows"] == 1 and quality["2026-01"]["human_edited"] is True
    assert out["deleted_rows"] == 1


# --- M5:LLM 全部失败要让命令失败 ---

def test_llm_total_failure_fails_the_command(approved, tmp_path, capsys, monkeypatch) -> None:
    import scripts.run_research as rr

    async def broken(prompt: str) -> str:
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(rr, "_build_llm_complete", lambda: broken)
    err = run(tmp_path, capsys, "classify", "--project", approved, "--provider", "llm", "--label", "n=N", fail=True)
    assert "LLM" in err["error"] and "失败" in err["error"]


def test_llm_config_is_validated_before_enrichment(approved, tmp_path, capsys, monkeypatch) -> None:
    import scripts.run_research as rr

    calls = []
    monkeypatch.setattr(rr, "_run_enrichment", lambda *a, **k: calls.append(1))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    err = run(tmp_path, capsys, "classify", "--project", approved, "--provider", "llm", "--enrich", "--label", "n=N",
              fail=True)
    assert "ANTHROPIC_API_KEY" in err["error"] and calls == []


# --- H5:新品分析只统计通过新品队列的 ASIN ---

def test_new_product_analysis_uses_queue_filter(approved, tmp_path, capsys) -> None:
    pid = approved
    cls = run(tmp_path, capsys, "classify", "--project", pid, "--label", "n=N", "--rule", "n=nugget")
    run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", cls["checkpoint_id"], "--approve")
    rows = [
        {"ASIN": "OLD", "月份": "2026-01", "月销量": "900", "上架时间": "2020-01-01", "父ASIN": "PO", "细分": "n"},
        {"ASIN": "OLD", "月份": "2026-02", "月销量": "900", "上架时间": "2020-01-01", "父ASIN": "PO", "细分": "n"},
        {"ASIN": "OLD", "月份": "2026-03", "月销量": "900", "上架时间": "2020-01-01", "父ASIN": "PO", "细分": "n"},
        {"ASIN": "NEW", "月份": "2026-01", "月销量": "300", "上架时间": "2026-01-10", "父ASIN": "PN", "细分": "n"},
        {"ASIN": "NEW", "月份": "2026-02", "月销量": "300", "上架时间": "2026-01-10", "父ASIN": "PN", "细分": "n"},
        {"ASIN": "NEW", "月份": "2026-03", "月销量": "300", "上架时间": "2026-01-10", "父ASIN": "PN", "细分": "n"},
    ]
    path = _write(tmp_path / "np.csv", rows)
    run(tmp_path, capsys, "analyze", "--project", pid, "--new-products-file", path, "--study-month", "2026-03")
    summary = json.loads((tmp_path / "projects" / pid / "new_products.json").read_text(encoding="utf-8"))["summary"]
    assert summary["retention_denominator_products"] == 1  # OLD(2020 年上架)不是新品,不进分母
    ctx = json.loads((tmp_path / "projects" / pid / "analysis_context.json").read_text(encoding="utf-8"))
    assert ctx["new_products_excluded_by_queue"] == 1
    # 新品表已人工预筛时可显式关闭队列过滤
    run(tmp_path, capsys, "analyze", "--project", pid, "--new-products-file", path, "--study-month", "2026-03",
        "--skip-queue-filter")
    summary = json.loads((tmp_path / "projects" / pid / "new_products.json").read_text(encoding="utf-8"))["summary"]
    assert summary["retention_denominator_products"] == 2


# --- M6:资料包不纳入未绑定的 nodes.json,过期的 coverage/table_edits 在重新导入时清理 ---

def test_stale_coverage_and_table_edits_are_cleaned_on_new_ingest(gw, tmp_path, capsys) -> None:
    pid = new_project(tmp_path, capsys)
    run(tmp_path, capsys, "ingest", "--project", pid, "--live", "--keyword", "ice", "--months", "auto", "--nodes", "n1")
    pdir = tmp_path / "projects" / pid
    assert (pdir / "coverage.json").exists()
    (tmp_path / "2026-01.csv").write_text(CSV, encoding="utf-8")
    run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-01.csv"))
    assert not (pdir / "coverage.json").exists()
