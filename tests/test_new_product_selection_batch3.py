import json

import pytest

from research_workflow.data.models import BSRRecord
from research_workflow.new_products import select_new_products
from scripts.run_research import main


def _r(asin, launch, parent="", month="2026-03", sales=100):
    return BSRRecord(asin, month, sales, parent_asin=parent, launch_date=launch)


def test_window_and_old_parent_and_unknown_dates():
    recs = [
        _r("NEW", "2026-02-10"),
        _r("OLD", "2024-05-01"),
        _r("VAR", "2026-02-20", parent="P1"),     # 新变体
        _r("MAIN", "2023-01-01", parent="P1"),    # 同父体老商品
        _r("NODATE", None),
        _r("FUTURE", "2026-04-02"),
        _r("EDGE", "2025-12-31"),                 # 3月31日-90天=2025-12-31，窗口起点本身不算新品
        _r("MONTHONLY", "2026-01"),
    ]
    got = {c.asin: c.reason for c in select_new_products(recs, "2026-03", 90)}
    assert got == {"NEW": "ok", "OLD": "not_in_window", "VAR": "variant_of_old_parent", "MAIN": "not_in_window",
                   "NODATE": "launch_date_unknown", "FUTURE": "after_cutoff", "EDGE": "not_in_window",
                   "MONTHONLY": "ok"}


def test_cli_select_exports_reviewable_tracking_table(tmp_path, capsys):
    def run(*argv):
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    csv = tmp_path / "2026-03.csv"
    csv.write_text("ASIN,月销量,上架时间,父ASIN\nNEW,100,2026-02-10,\nOLD,500,2022-01-01,\nNODATE,20,,\n", encoding="utf-8")
    cp = run("ingest", "--project", pid, "--file", str(csv))["checkpoint_id"]
    run("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    out = run("new-products-select", "--project", pid, "--output", str(tmp_path / "np.csv"), "--include-excluded")
    assert out["eligible_asins"] == 1
    assert out["reasons"] == {"ok": 1, "not_in_window": 1, "launch_date_unknown": 1}
    text = (tmp_path / "np.csv").read_text(encoding="utf-8-sig")
    assert "NEW" in text and "launch_date_unknown" in text
