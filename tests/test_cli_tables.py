"""T1-9:表1/表2/表3 各自导出 → 人工修改 → 上传校验 → 旧审批失效 → 重新确认。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.run_research import main

CSV_MONTH = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地
A1,90,B1,Nugget Ice Maker,1,CN
A2,60,B2,Bullet Ice Maker,2,
A3,30,B3,Other Ice Thing,3,US
"""


def _run(tmp_path, capsys, *argv):
    capsys.readouterr()
    main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
    return json.loads(capsys.readouterr().out)


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture()
def project(tmp_path, capsys):
    (tmp_path / "2026-01.csv").write_text(CSV_MONTH, encoding="utf-8")
    pid = _run(tmp_path, capsys, "init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = _run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-01.csv"))
    return pid, out["checkpoint_id"]


def _approve(tmp_path, capsys, pid, cp, *extra):
    return _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", cp, "--approve", *extra)


def test_table1_edit_invalidates_old_approval_and_requires_reapproval(project, tmp_path, capsys) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    out = tmp_path / "t1.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "1", "--output", str(out))
    rows = _read(out)
    assert {r["asin"] for r in rows} == {"A1", "A2", "A3"}
    for row in rows:
        if row["asin"] == "A2":
            row["monthly_sales"] = "200"  # 人工修正销量
            row["seller_region"] = "US"
    rows = [r for r in rows if r["asin"] != "A3"]  # 人工剔除异常行
    imported = _run(tmp_path, capsys, "import-table", "--project", pid, "--table", "1",
                    "--file", str(_write(tmp_path / "t1-edit.csv", rows)))
    assert imported["checkpoint_step"] == "data_review" and imported["changed_cells"] >= 2
    assert imported["deleted_rows"] == 1

    records = json.loads((tmp_path / "projects" / pid / "records.json").read_text(encoding="utf-8"))["2026-01"]
    by_asin = {r["asin"]: r for r in records}
    assert set(by_asin) == {"A1", "A2"} and by_asin["A2"]["monthly_sales"] == 200
    assert by_asin["A2"]["sales_rank"] == 1 and by_asin["A1"]["sales_rank"] == 2  # 修改后重排名次

    # 旧审批已失效:未重新确认前不能继续下游
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "classify", "--project", pid, "--label", "x=X")
    _approve(tmp_path, capsys, pid, imported["checkpoint_id"])
    assert _run(tmp_path, capsys, "classify", "--project", pid, "--label", "x=X")["total_asins"] == 2


@pytest.mark.parametrize("mutate,message", [
    (lambda rows: rows + [{**rows[0], "asin": "NEW1"}], "不存在"),
    (lambda rows: [{**rows[0], "monthly_sales": ""}] + rows[1:], "销量"),
    (lambda rows: [{**rows[0], "monthly_sales": "-5"}] + rows[1:], "销量"),
    (lambda rows: [{**rows[0], "extra_col": "x"}] + rows[1:], "未知列"),
    (lambda rows: rows + [rows[0]], "重复"),
])
def test_table1_import_validation_rejects_bad_files(project, tmp_path, capsys, mutate, message) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    out = tmp_path / "t1.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "1", "--output", str(out))
    bad = _write(tmp_path / "bad.csv", mutate(_read(out)))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), "import-table",
              "--project", pid, "--table", "1", "--file", str(bad)])
    assert message in json.loads(capsys.readouterr().out)["error"]
    # 校验失败不改变任何状态:原审批仍有效
    assert _run(tmp_path, capsys, "classify", "--project", pid, "--label", "x=X")["total_asins"] == 3


def test_table3_edit_resets_classification_approval(project, tmp_path, capsys) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    first = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=N", "--label", "bullet=B",
                 "--rule", "nugget=nugget", "--rule", "bullet=bullet")
    _approve(tmp_path, capsys, pid, first["checkpoint_id"])
    _run(tmp_path, capsys, "analyze", "--project", pid)

    out = tmp_path / "t3.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "3", "--output", str(out))
    rows = _read(out)
    assert {"asin", "label", "needs_review"} <= set(rows[0])
    for row in rows:
        if row["asin"] == "A3":
            row["label"] = "bullet"
    imported = _run(tmp_path, capsys, "import-table", "--project", pid, "--table", "3",
                    "--file", str(_write(tmp_path / "t3-edit.csv", rows)))
    assert imported["checkpoint_step"] == "classification_review" and imported["changed_labels"] == 1
    envelope = json.loads((tmp_path / "projects" / pid / "classification.json").read_text(encoding="utf-8"))
    assert envelope["batch"]["rows"]["A3"]["label"] == "bullet" and envelope["batch"]["approved"] is False
    # 旧的分析结果与审批失效
    assert not (tmp_path / "projects" / pid / "market_metrics.json").exists()
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "analyze", "--project", pid)
    _approve(tmp_path, capsys, pid, imported["checkpoint_id"])
    _run(tmp_path, capsys, "analyze", "--project", pid)


def test_table3_rejects_invalid_label_and_mismatched_asins(project, tmp_path, capsys) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=N", "--rule", "nugget=nugget")
    out = tmp_path / "t3.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "3", "--output", str(out))
    rows = _read(out)
    for bad_rows, message in (([{**rows[0], "label": "made-up"}, *rows[1:]], "标签"), (rows[:-1], "ASIN")):
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), "import-table",
                  "--project", pid, "--table", "3", "--file", str(_write(tmp_path / "bad3.csv", bad_rows))])
        assert message in json.loads(capsys.readouterr().out)["error"]


def test_table2_edits_survive_reclassification_and_fill_records(project, tmp_path, capsys) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    out = tmp_path / "t2.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(out))
    rows = _read(out)
    assert {"asin", "seller_region", "bullets"} <= set(rows[0]) and len(rows) == 3
    for row in rows:
        if row["asin"] == "A2":
            row["seller_region"] = "CN"
            row["bullets"] = "Makes bullet ice fast || Compact"
    imported = _run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2",
                    "--file", str(_write(tmp_path / "t2-edit.csv", rows)))
    assert imported["checkpoint_step"] == "enrichment_review"  # 表2 修改须经补齐检查点重新确认
    _approve(tmp_path, capsys, pid, imported["checkpoint_id"])
    # 表2 只让分类失效,表1 审批保持有效
    for _ in range(2):  # 重复分类后人工修改仍在
        res = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "bullet=B",
                   "--rule", "bullet=fast")
        assert res["label_counts"].get("bullet") == 1  # A2 的卖点含 fast,命中规则
    _approve(tmp_path, capsys, pid, res["checkpoint_id"])
    _run(tmp_path, capsys, "analyze", "--project", pid)
    market = (tmp_path / "projects" / pid / "market_metrics.json").read_text(encoding="utf-8")
    assert '"UNKNOWN"' not in market  # A2 的地区已由人工补为 CN


def test_table2_upload_without_machine_enrichment_still_requires_review_before_classify(project, tmp_path, capsys) -> None:
    pid, cp = project
    _approve(tmp_path, capsys, pid, cp)
    out = tmp_path / "t2.csv"
    _run(tmp_path, capsys, "export-table", "--project", pid, "--table", "2", "--output", str(out))
    rows = _read(out)
    for row in rows:
        if row["asin"] == "A2":
            row["bullets"] = "Makes bullet ice fast"
    imported = _run(tmp_path, capsys, "import-table", "--project", pid, "--table", "2",
                    "--file", str(_write(tmp_path / "t2-edit.csv", rows)))
    assert _run(tmp_path, capsys, "status", "--project", pid)["next"] == "resolve"
    with pytest.raises(SystemExit):  # 人工表2 未经补齐检查点确认:不能分类
        _run(tmp_path, capsys, "classify", "--project", pid, "--label", "bullet=B", "--rule", "bullet=fast")
    _approve(tmp_path, capsys, pid, imported["checkpoint_id"])
    assert _run(tmp_path, capsys, "status", "--project", pid)["next"] == "classify"
    res = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "bullet=B", "--rule", "bullet=fast")
    assert res["label_counts"].get("bullet") == 1
