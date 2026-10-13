"""外部证据账本：来源、单位、覆盖、结论边界。"""
from __future__ import annotations

import json

import pytest

from research_workflow.data.models import BSRRecord
from research_workflow.evidence import EvidenceLedger, required_slots, sample_share_of_market
from scripts.run_research import main

SRC = {"source_name": "Statista", "source_url": "https://example.com/report"}


def _ledger(tmp_path):
    return EvidenceLedger(tmp_path / "ledger.json")


def test_evidence_requires_traceable_source_and_valid_slot(tmp_path):
    l = _ledger(tmp_path)
    with pytest.raises(ValueError, match="未知证据槽位"):
        l.add_evidence(slot="nope", claim="x", **SRC)
    with pytest.raises(ValueError, match="可追溯来源"):
        l.add_evidence(slot="google_trends", claim="x", source_name="n")
    with pytest.raises(ValueError, match="http"):
        l.add_evidence(slot="google_trends", claim="x", source_name="n", source_url="ftp://x")
    with pytest.raises(ValueError, match="SHA256"):
        l.add_evidence(slot="google_trends", claim="x", source_name="n", source_file="raw/a.pdf")


def test_tiktok_sales_must_be_units_not_gmv(tmp_path):
    l = _ledger(tmp_path)
    base = dict(slot="tiktok_units_us_overall", claim="2025 US TikTok", period="2025", **SRC)
    with pytest.raises(ValueError, match="台数"):
        l.add_evidence(value=1_000_000, unit="GMV", **base)
    with pytest.raises(ValueError, match="台数"):
        l.add_evidence(value=1_000_000, unit="USD", **base)
    with pytest.raises(ValueError, match="数值"):
        l.add_evidence(value=None, unit="台数", **base)
    rec = l.add_evidence(value="1,200,000", unit="台数", **base)
    assert rec.unit == "units" and rec.value == 1_200_000.0


def test_duplicate_evidence_rejected_and_ids_are_stable(tmp_path):
    l = _ledger(tmp_path)
    a = l.add_evidence(slot="google_trends", claim="持续上升", **SRC)
    with pytest.raises(ValueError, match="已存在"):
        l.add_evidence(slot="google_trends", claim="持续上升", **SRC)
    l.save()
    assert EvidenceLedger(tmp_path / "ledger.json").evidence[0].evidence_id == a.evidence_id


def test_findings_need_existing_same_module_evidence_and_no_decision_wording(tmp_path):
    l = _ledger(tmp_path)
    ev = l.add_evidence(slot="google_trends", claim="近三年搜索热度上升", **SRC)
    patent = l.add_evidence(slot="patents", claim="存在外观专利", **SRC)
    with pytest.raises(ValueError, match="至少一条"):
        l.add_finding(module="market_growth", text="热度上升", evidence_ids=[])
    with pytest.raises(ValueError, match="不存在"):
        l.add_finding(module="market_growth", text="热度上升", evidence_ids=["ev:bad"])
    with pytest.raises(ValueError, match="不属于模块"):
        l.add_finding(module="market_growth", text="热度上升", evidence_ids=[patent.evidence_id])
    with pytest.raises(ValueError, match="决策边界"):
        l.add_finding(module="market_growth", text="建议进入该市场", evidence_ids=[ev.evidence_id])
    f = l.add_finding(module="market_growth", text="近三年搜索热度上升", evidence_ids=[ev.evidence_id])
    assert f.evidence_ids == (ev.evidence_id,)


def test_coverage_baseline_plus_confirmed_route_slots(tmp_path):
    l = _ledger(tmp_path)
    keys = {s.key for s in required_slots([])}
    assert {"market_size_us", "tiktok_units_us_overall", "patents"} <= keys
    assert "channel_erosion" not in keys
    cov = {c.slot: c for c in l.coverage(["external_channels"])}
    assert cov["channel_erosion"].status == "missing"
    l.add_evidence(slot="channel_erosion", claim="线下渠道增长", **SRC)
    assert {c.slot: c for c in l.coverage(["external_channels"])}["channel_erosion"].status == "covered"


def test_sample_share_needs_units_annual_evidence_and_full_year(tmp_path):
    recs = [BSRRecord("A", f"2025-{m:02d}", 100) for m in range(1, 13)]
    l = _ledger(tmp_path)
    assert sample_share_of_market(recs, l.evidence)["share_pct"] is None
    l.add_evidence(slot="market_size_us", claim="美国冰机年销量", value=10000, unit="units", period="2025", **SRC)
    out = sample_share_of_market(recs, l.evidence)
    assert out["share_pct"] == 12.0 and out["sample_sales"] == 1200
    partial = sample_share_of_market(recs[:6], l.evidence)
    assert partial["share_pct"] is None and "6/12" in partial["reason"]


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


def test_cli_evidence_flow_reaches_dossier_and_flags_gaps(cli):
    pid = cli("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    f = cli.tmp / "2026-01.csv"
    f.write_text("ASIN,月销量,品牌,商品标题,小类BSR\nA,100,X,a nugget,1\nB,50,Y,b,2\n", encoding="utf-8")
    cp = cli("ingest", "--project", pid, "--file", str(f))["checkpoint_id"]
    cli("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    cp = cli("classify", "--project", pid, "--label", "n=n", "--rule", "n=nugget")["checkpoint_id"]
    cli("resolve", "--project", pid, "--checkpoint", cp, "--approve")
    cp = cli("analyze", "--project", pid)["checkpoint_id"]
    cli("resolve", "--project", pid, "--checkpoint", cp, "--approve", "--select", "")

    assert "台数" in cli.fail("evidence-add", "--project", pid, "--slot", "tiktok_units_us_overall",
                             "--claim", "c", "--value", "5", "--unit", "GMV", "--source-name", "n",
                             "--source-url", "https://e.com")
    ev = cli("evidence-add", "--project", pid, "--slot", "google_trends", "--claim", "近三年搜索热度上升",
             "--source-name", "Google Trends", "--source-url", "https://trends.google.com/x")["evidence"]
    cli("finding-add", "--project", pid, "--module", "market_growth", "--text", "搜索热度近三年上升",
        "--evidence", ev["evidence_id"])
    status = cli("evidence-status", "--project", pid)
    assert "google_trends" not in status["missing_slots"] and "market_size_us" in status["missing_slots"]

    out = cli("dossier", "--project", pid, "--output", str(cli.tmp / "out"))
    report = (cli.tmp / "out" / "report.md").read_text(encoding="utf-8")
    assert "## 7. 外部证据与深挖结论" in report and "搜索热度近三年上升" in report
    assert "**未覆盖**" in report and "样本销量不得当作美国整体市场规模" in report
    assert "evidence/evidence_ledger.json" in out["files"]
