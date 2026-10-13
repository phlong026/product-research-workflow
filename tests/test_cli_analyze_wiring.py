"""analyze 对新品双阈值/研究月/父体、新品队列与产品迭代进程的接线(T3-3/T3-4/T3-5/T2-4)。"""
from __future__ import annotations

import json
from datetime import date

from scripts.run_research import main

MONTH_CSV = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地,上架时间
P1,300,B1,Nugget Ice Maker,1,CN,2026-01-10
P2,100,B2,Bullet Ice Maker,2,US,2026-02-20
"""
NEW_CSV = """ASIN,月份,月销量,上架时间,卖家所属地,账号类型,细分,父ASIN
P1,2026-01,300,2026-01-10,CN,SC,nugget,PA1
P1,2026-02,300,2026-01-10,CN,SC,nugget,PA1
P1,2026-03,300,2026-01-10,CN,SC,nugget,PA1
P2,2026-03,100,2026-02-20,US,SC,bullet,PA2
"""


def _run(tmp_path, capsys, *argv):
    capsys.readouterr()
    main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
    return json.loads(capsys.readouterr().out)


def test_analyze_wires_retention_queue_and_iteration(tmp_path, capsys, monkeypatch) -> None:
    import scripts.run_research as rr

    monkeypatch.setattr(rr, "_today", lambda: date(2026, 4, 1))
    (tmp_path / "2026-03.csv").write_text(MONTH_CSV, encoding="utf-8")
    (tmp_path / "new.csv").write_text(NEW_CSV, encoding="utf-8")
    pid = _run(tmp_path, capsys, "init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = _run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-03.csv"))
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    out = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=Nugget", "--label", "bullet=Bullet",
               "--rule", "nugget=nugget", "--rule", "bullet=bullet")
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    _run(tmp_path, capsys, "analyze", "--project", pid, "--new-products-file", str(tmp_path / "new.csv"),
         "--promotion-threshold", "200", "--retention-threshold", "150", "--study-month", "2026-03")

    pdir = tmp_path / "projects" / pid
    summary = json.loads((pdir / "new_products.json").read_text(encoding="utf-8"))["summary"]
    assert summary["promotion_waist_sales_threshold"] == 200 and summary["retention_sales_threshold"] == 150
    assert summary["retention_denominator_products"] == 2 and summary["retained_products"] == 1

    queue = json.loads((pdir / "new_product_queue.json").read_text(encoding="utf-8"))
    assert {item["asin"] for item in queue["items"]} == {"P1", "P2"}

    iteration = json.loads((pdir / "iteration.json").read_text(encoding="utf-8"))
    by_segment = {s["segment"]: s for s in iteration["segments"]}
    assert by_segment["nugget"]["earliest_launch_date"] == "2026-01-10"
    assert by_segment["bullet"]["earliest_launch_date"] == "2026-02-20"
    assert "earliest_observed_in_data" in by_segment["nugget"]
