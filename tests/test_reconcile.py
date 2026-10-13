"""指标与人工结果逐项对账(验收工具)。"""
from __future__ import annotations

import json

import pytest

from research_workflow.reconcile import flatten_metrics, reconcile, summarize
from scripts.run_research import main


def test_flatten_and_reconcile_statuses():
    market = {"market": [{"month": "2026-01", "total_sales": 150, "yoy_pct": None}],
              "segments": [{"month": "2026-01", "segment": "nugget", "sales": 100}]}
    comp = {"concentration": [{"month": "2026-01", "top3_asin_share_pct": 100.0}],
            "point_rank_matrix": {"rows": [{"month": "2026-01", "point_sales": {"1": 100, "2": 50, "5": None}}]}}
    flat = flatten_metrics(market, comp, {"summary": {"retention_rate_pct": 25.0}})
    assert flat["market.2026-01.total_sales"] == 150
    assert flat["segments.2026-01.nugget.sales"] == 100
    assert flat["point_rank.2026-01.2.sales"] == 50 and flat["point_rank.2026-01.5.sales"] is None
    rows = reconcile([
        {"key": "market.2026-01.total_sales", "expected": "150"},
        {"key": "segments.2026-01.nugget.sales", "expected": 98, "tolerance_pct": 5},
        {"key": "concentration.2026-01.top3_asin_share_pct", "expected": 99.0},
        {"key": "point_rank.2026-01.5.sales", "expected": 0},   # 实际是未知,不是 0
        {"key": "market.2026-01.yoy_pct", "expected": 5},        # 实际 None
        {"key": "new_products.summary.retention_rate_pct", "expected": "25.00%"},
        {"key": "nope.key", "expected": 1},
    ], flat)
    assert [r.status for r in rows] == ["MATCH", "MATCH", "MISMATCH", "MISSING", "MISSING", "MATCH", "MISSING"]
    assert summarize(rows) == {"total": 7, "MATCH": 3, "MISMATCH": 1, "MISSING": 3}


def test_duplicate_or_blank_expected_rejected():
    with pytest.raises(ValueError, match="重复"):
        reconcile([{"key": "a", "expected": 1}, {"key": "a", "expected": 2}], {"a": 1})
    with pytest.raises(ValueError, match="缺失或非法"):
        reconcile([{"key": "a", "expected": ""}], {"a": 1})


def test_cli_reconcile_end_to_end(tmp_path, capsys):
    def run(*argv):
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    f = tmp_path / "2026-01.csv"
    f.write_text("ASIN,月销量,品牌,商品标题,小类BSR\nA,100,X,a nugget,1\nB,50,Y,b,2\n", encoding="utf-8")
    run("resolve", "--project", pid, "--checkpoint", run("ingest", "--project", pid, "--file", str(f))["checkpoint_id"], "--approve")
    cp = run("classify", "--project", pid, "--label", "n=n", "--rule", "n=nugget")["checkpoint_id"]
    run("resolve", "--project", pid, "--checkpoint", cp, "--approve", "--edit", "B=n")
    run("analyze", "--project", pid)
    ref = tmp_path / "ref.csv"
    ref.write_text("key,expected,tolerance_abs\nmarket.2026-01.total_sales,150,0\npoint_rank.2026-01.2.sales,50,0\n"
                   "point_rank.2026-01.5.sales,0,0\n", encoding="utf-8")
    out = run("reconcile", "--project", pid, "--reference", str(ref), "--output", str(tmp_path / "rep.csv"))
    assert out["summary"] == {"total": 3, "MATCH": 2, "MISMATCH": 0, "MISSING": 1}
    assert out["mismatches"][0]["key"] == "point_rank.2026-01.5.sales"
    assert (tmp_path / "rep.csv").exists()


def test_web_job_whitelist_accepts_reconcile_and_rejects_extra_params(tmp_path):
    from research_workflow.web import jobs

    parsed = jobs.validate_params("reconcile", {"upload": "abc123"})
    argv = jobs.build_argv("reconcile", parsed, project_id="p1", project_dir=tmp_path,
                           upload_path=lambda u: tmp_path / "uploads" / u)
    assert argv[0] == "reconcile" and f"--reference={tmp_path / 'uploads' / 'abc123'}" in argv
    assert argv[-1] == f"--output={tmp_path / 'exports' / 'reconcile.csv'}"
    with pytest.raises(jobs.JobValidationError):
        jobs.validate_params("reconcile", {"upload": "abc", "output": "/etc/passwd"})


def test_web_job_whitelist_has_enrich_and_no_longer_accepts_classify_enrich_flag(tmp_path):
    from research_workflow.web import jobs

    argv = jobs.build_argv("enrich", jobs.validate_params("enrich", {}), project_id="p1", project_dir=tmp_path,
                           upload_path=lambda u: tmp_path / u)
    assert argv == ["enrich", "--project=p1"]
    with pytest.raises(jobs.JobValidationError):
        jobs.validate_params("classify", {"labels": {"n": "N"}, "enrich": True})
    with pytest.raises(jobs.JobValidationError):
        jobs.validate_params("enrich", {"live": True})
