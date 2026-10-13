"""三张过程表导出/上传、差异留痕，以及综合 Excel 成品。"""
from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from scripts.run_research import main


@pytest.fixture()
def cli(tmp_path, capsys):
    def run(*argv):
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    def fail(*argv):
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)["error"]

    run.fail, run.tmp = fail, tmp_path
    return run


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


CSV = "ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地,评分,评分数\nN1,100,NB,Nugget Maker,1,CN,4.5,100\nB1,50,BB,Bullet Maker,2,US,4.1,40\n"


def _start(cli):
    pid = cli("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = cli("ingest", "--project", pid, "--file", _w(cli.tmp, "2026-01.csv", CSV))
    return pid, out["checkpoint_id"]


def _edit_b1_sales(xlsx, value):
    wb = load_workbook(xlsx)
    ws = wb["表1"]
    headers = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[headers.index("ASIN")].value == "B1":
            row[headers.index("月销量")].value = value
    wb.save(xlsx)


def test_table1_export_edit_import_creates_audit_and_new_checkpoint(cli):
    pid, cp = _start(cli)
    xlsx = cli.tmp / "t1.xlsx"
    assert cli("table-export", "--project", pid, "--table", "1", "--output", str(xlsx))["rows"] == 2

    # 未改动 => 拒绝，不制造空版本
    assert "完全一致" in cli.fail("table-import", "--project", pid, "--table", "1", "--file", str(xlsx))

    _edit_b1_sales(xlsx, 70)
    out = cli("table-import", "--project", pid, "--table", "1", "--file", str(xlsx))
    assert out["checkpoint_step"] == "data_review" and out["checkpoint_id"] != cp
    pdir = cli.tmp / "projects" / pid
    recs = json.loads((pdir / "records.json").read_text(encoding="utf-8"))["2026-01"]
    assert {r["asin"]: r["monthly_sales"] for r in recs} == {"N1": 100, "B1": 70}
    log = json.loads((pdir / "table_edits.json").read_text(encoding="utf-8"))
    assert log[0]["changes"] == [{"month": "2026-01", "asin": "B1", "field": "monthly_sales", "from": 50, "to": 70}]
    sources = json.loads((pdir / "sources.json").read_text(encoding="utf-8"))
    assert any(v["kind"] == "human_table" for v in sources.values())
    # 旧检查点已失效，只能裁决新的
    assert "过期" in cli.fail("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")


def test_table1_import_rejects_missing_sales_and_month_changes(cli):
    pid, _ = _start(cli)
    xlsx = cli.tmp / "t1.xlsx"
    cli("table-export", "--project", pid, "--table", "1", "--output", str(xlsx))
    _edit_b1_sales(xlsx, None)
    assert "不能视为 0" in cli.fail("table-import", "--project", pid, "--table", "1", "--file", str(xlsx))
    csv = cli.tmp / "t1.csv"
    cli("table-export", "--project", pid, "--table", "1", "--output", str(csv))
    text = csv.read_text(encoding="utf-8-sig").replace("2026-01", "2026-02")
    csv.write_text(text, encoding="utf-8-sig")
    assert "月份集合" in cli.fail("table-import", "--project", pid, "--table", "1", "--file", str(csv))


def test_table3_edits_file_only_applies_changed_rows(cli):
    pid, cp = _start(cli)
    cli("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    out = cli("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--label", "bullet=子弹冰",
              "--rule", "nugget=nugget")
    t3 = cli.tmp / "t3.csv"
    cli("table-export", "--project", pid, "--table", "3", "--output", str(t3))
    text = t3.read_text(encoding="utf-8-sig")
    assert "B1" in text and "unknown" in text
    lines = text.splitlines()
    header = lines[0].split(",")
    i_asin, i_cls = header.index("ASIN"), header.index("分类")
    new = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        if cells[i_asin] == "B1":
            cells[i_cls] = "bullet"
        new.append(",".join(cells))
    t3.write_text("\n".join(new) + "\n", encoding="utf-8-sig")
    out = cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve", "--edits-file", str(t3))
    assert out["edits"] == {"B1": "bullet"}


def test_dossier_includes_editable_workbook_with_blank_human_decision(cli):
    pid, cp = _start(cli)
    cli("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    out = cli("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--label", "bullet=子弹冰", "--rule", "nugget=nugget")
    cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve", "--edit", "B1=bullet")
    out = cli("analyze", "--project", pid)
    cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve", "--select", "")
    dossier = cli.tmp / "out"
    result = cli("dossier", "--project", pid, "--output", str(dossier))
    assert "workbook.xlsx" in result["files"]
    wb = load_workbook(dossier / "workbook.xlsx")
    assert {"说明", "原始数据", "表1_清洗Top", "表3_分类", "市场月度", "集中度", "逐名次销量"} <= set(wb.sheetnames)
    notes = {r[0].value: r[1].value for r in wb["说明"].iter_rows(min_row=2)}
    assert notes["人工决策（Yes/No）"] in (None, "")
    manifest = json.loads((dossier / "manifest.json").read_text(encoding="utf-8"))
    assert "workbook.xlsx" in {f["path"] for f in manifest["files"]}
    raw_sheet = wb["原始数据"]
    assert raw_sheet.max_row == 3  # 表头 + 两条原始候选行


# --- 评审意见回归 ---

def test_allow_month_change_really_removes_omitted_month(cli):
    pid = cli("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    f1 = _w(cli.tmp, "2026-01.csv", CSV)
    f2 = _w(cli.tmp, "2026-02.csv", CSV.replace("100", "120"))
    cli("ingest", "--project", pid, "--file", f1, "--file", f2)
    csv = cli.tmp / "t1.csv"
    cli("table-export", "--project", pid, "--table", "1", "--output", str(csv))
    kept = [ln for i, ln in enumerate(csv.read_text(encoding="utf-8-sig").splitlines()) if i == 0 or not ln.startswith("2026-02")]
    csv.write_text("\n".join(kept) + "\n", encoding="utf-8-sig")
    assert "月份集合" in cli.fail("table-import", "--project", pid, "--table", "1", "--file", str(csv))
    out = cli("table-import", "--project", pid, "--table", "1", "--file", str(csv), "--allow-month-change")
    assert set(out["months"]) == {"2026-01"}
    pdir = cli.tmp / "projects" / pid
    assert set(json.loads((pdir / "records.json").read_text(encoding="utf-8"))) == {"2026-01"}
    assert set(json.loads((pdir / "quality.json").read_text(encoding="utf-8"))) == {"2026-01"}
    assert all(not k.startswith("2026-02") for k in json.loads((pdir / "sources.json").read_text(encoding="utf-8")))
    assert set(json.loads((pdir / "candidates.json").read_text(encoding="utf-8"))) == {"2026-01"}


def test_workbook_text_starting_with_equals_is_a_literal_not_a_formula(tmp_path):
    from research_workflow.data.xlsx import write_xlsx

    path = write_xlsx(tmp_path / "x.xlsx", {"s": [{"标题": '=HYPERLINK("http://evil","x")', "n": 5, "plus": "+1+1", "at": "@SUM(1)"}]})
    ws = load_workbook(path)["s"]
    cell = ws["A2"]
    assert cell.data_type == "s" and cell.value == '=HYPERLINK("http://evil","x")'
    assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)
    assert ws["B2"].value == 5


def test_csv_export_neutralizes_formulas_but_roundtrips_exactly(tmp_path):
    from research_workflow.data import read_tabular_rows, write_csv_rows

    rows = [{"标题": "=cmd|' /C calc'!A0", "品牌": "-X Brand", "销量": -5, "ok": "plain"}]
    path = write_csv_rows(tmp_path / "t.csv", rows)
    raw = path.read_text(encoding="utf-8-sig")
    assert "'=cmd" in raw and "'-X Brand" in raw       # 打开 CSV 不会被当公式
    back = read_tabular_rows(path)[0]
    assert back["标题"] == "=cmd|' /C calc'!A0" and back["品牌"] == "-X Brand" and back["ok"] == "plain"


def test_cli_help_strings_are_valid_for_argparse_formatting():
    import argparse

    from scripts.run_research import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, sp in sub.choices.items():
        sp.format_help()  # 帮助文本里含未转义 % 会在这里抛 ValueError（Python 3.14 会在构建解析器时就抛）
