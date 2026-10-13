"""外部证据接线:import-evidence / build-market-forecast / build-cross-channel(证据登记,不是检查点)。"""
from __future__ import annotations

import json
import re
from hashlib import sha256

import pytest

from research_workflow.competition_external.ledger import assert_no_decision_language
from tests.external_data import (
    ALL_KINDS,
    EXCERPTS_CSV_2,
    EXCERPTS_NO_CLAIM_ID,
    EXCERPTS_NO_SOURCE,
    TIKTOK_GMV_CSV,
    TIKTOK_META,
    TRENDS_META,
    Cli,
    analyzed_project,
    tree_digest,
)


@pytest.fixture()
def cli(tmp_path, capsys) -> Cli:
    return Cli(tmp_path, capsys)


@pytest.fixture()
def pid(cli) -> str:
    return analyzed_project(cli)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ import-evidence


def test_trends_import_snapshots_stores_and_indexes(cli, pid) -> None:
    out = cli.import_sample(pid, "trends")
    pdir = cli.pdir(pid)
    digest = sha256((cli.tmp_path / "samples" / "trends.csv").read_bytes()).hexdigest()
    assert out["kind"] == "trends" and out["seq"] == 1 and out["sha256"] == digest and out["records"] == 14
    assert (pdir / "raw" / f"{digest}.csv").is_file()  # 原文件按 SHA256 快照到 raw/

    stored = _read(pdir / "external" / "trends.json")
    entry = stored["entries"][0]
    assert entry["seq"] == 1 and entry["sha256"] == digest and entry["record_count"] == 14
    series = entry["data"]["series"][0]
    assert series["keyword"] == "ice maker" and len(series["monthly"]) == 14
    assert entry["data"]["geo"] == "US" and entry["data"]["source_name"] == "Google Trends"

    index = _read(pdir / "external" / "index.json")
    row = index["entries"][0]
    assert {"kind", "file_name", "sha256", "imported_at", "record_count", "summary"} <= set(row)
    assert row["kind"] == "trends" and row["file_name"] == "trends.csv" and row["snapshot"] == f"raw/{digest}.csv"
    actual = sha256((pdir / "external" / "trends.json").read_bytes()).hexdigest()
    assert index["files"]["trends.json"] == actual  # 结构化结果文件的哈希也登记在索引里


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_kind_imports_with_records(cli, pid, kind) -> None:
    if kind == "patent-search-log":
        cli.import_sample(pid, "patents")  # 日志需要挂接已导入的专利记录
    out = cli.import_sample(pid, kind)
    assert out["records"] >= 1 and out["summary"]
    stored = _read(cli.pdir(pid) / "external" / f"{kind}.json")
    assert stored["kind"] == kind and stored["entries"][0]["data"]
    assert "不是人工检查点" in out["note"]


def test_patent_search_log_distinguishes_not_searched_from_none(cli, pid) -> None:
    cli.import_sample(pid, "patents")
    cli.import_sample(pid, "patent-search-log")
    logs = _read(cli.pdir(pid) / "external" / "patent-search-log.json")["entries"][0]["data"]
    by_query = {log["query"]: log for log in logs}
    assert by_query["ice maker nugget"]["record_count"] == 1
    assert by_query["ice maker bullet"]["status_label"] == "已检索-范围内未发现"
    assert by_query["ice maker clear"]["status_label"] == "未检索"
    assert all("不构成法律意见" in log["disclaimer"] for log in logs)


@pytest.mark.parametrize(
    "kind,content,meta,message",
    [
        ("trends", "date,keyword,value\n2025-01-01,x,1\n", [m for m in TRENDS_META if not m.startswith("source_url")], "source_url"),
        ("trends", "date,keyword,value\n2025-01-01,x,1\n", [*TRENDS_META, "bogus=1"], "bogus"),
        ("tiktok-units", TIKTOK_GMV_CSV, TIKTOK_META, "GMV"),
        ("tiktok-units", "geo,segment,channel,period,units,is_forecast,evidence_id\n"
                         "us,countertop,tiktok,2024,1,false,e1\n", TIKTOK_META[:2], "as_of"),
        ("report-excerpts", EXCERPTS_NO_SOURCE, [], "source"),
        ("patent-search-log", "query,database,scope,status,searched_at\nq,USPTO,US,searched_found,2026-10-01\n", [],
         "PatentRecord"),
        ("patents", "patent_no,title,source_name,retrieved_at,url\nUS1,t,USPTO,2026-10-01,https://x\n", [], "related_asin"),
    ],
)
def test_failed_import_reports_error_and_changes_nothing(cli, pid, kind, content, meta, message) -> None:
    before = tree_digest(cli.pdir(pid))
    state_before = cli.run("status", "--project", pid)
    out = cli.fail("import-evidence", "--project", pid, "--kind", kind, "--file", str(cli.write("bad.csv", content)),
                   *[a for m in meta for a in ("--meta", m)])
    assert message in out["error"] and out["command"] == "import-evidence"
    assert tree_digest(cli.pdir(pid)) == before  # 没有 external/、没有新快照
    assert not (cli.pdir(pid) / "external").exists()
    assert cli.run("status", "--project", pid) == state_before


def test_missing_file_and_unknown_project_are_errors(cli, pid) -> None:
    assert "文件不存在" in cli.fail("import-evidence", "--project", pid, "--kind", "ads", "--file", "/nope/ads.json")["error"]
    assert "Unknown project" in cli.fail("import-evidence", "--project", "nope", "--kind", "ads", "--file", "/nope")["error"]


def test_repeat_import_appends_numbered_entries_without_overwriting(cli, pid) -> None:
    cli.import_sample(pid, "report-excerpts")
    first = _read(cli.pdir(pid) / "external" / "report-excerpts.json")["entries"][0]
    second_out = cli.import_file(pid, "report-excerpts", cli.write("excerpts2.csv", EXCERPTS_CSV_2), [])
    assert second_out["seq"] == 2
    entries = _read(cli.pdir(pid) / "external" / "report-excerpts.json")["entries"]
    assert [e["seq"] for e in entries] == [1, 2] and entries[0] == first  # 历史条目原样保留
    index = _read(cli.pdir(pid) / "external" / "index.json")
    assert [(r["kind"], r["seq"]) for r in index["entries"]] == [("report-excerpts", 1), ("report-excerpts", 2)]


def test_same_file_twice_is_flagged_and_default_claim_ids_stay_unique(cli, pid) -> None:
    path = cli.write("noid.csv", EXCERPTS_NO_CLAIM_ID)
    cli.import_file(pid, "report-excerpts", path, [])
    out = cli.import_file(pid, "report-excerpts", path, [])  # 库默认编号 report:1 与首次冲突 -> 自动加导入序号
    assert out["seq"] == 2 and "内容相同" in out["summary"]
    entries = _read(cli.pdir(pid) / "external" / "report-excerpts.json")["entries"]
    ids = [item["claim_id"] for e in entries for item in e["data"]]
    assert ids == ["report:1", "report:s2:1"]


def test_import_is_not_a_checkpoint_and_does_not_touch_approved_artifacts(cli, tmp_path) -> None:
    pid = analyzed_project(cli, confirm_route=True)
    pdir = cli.pdir(pid)
    reviewed = {n: sha256((pdir / n).read_bytes()).hexdigest()
                for n in ("records.json", "classification.json", "market_metrics.json", "route_plan.json")}
    state_before = cli.run("status", "--project", pid)
    for kind in ("trends", "tiktok-units"):
        cli.import_sample(pid, kind)
    assert {n: sha256((pdir / n).read_bytes()).hexdigest() for n in reviewed} == reviewed
    state_after = cli.run("status", "--project", pid)
    assert state_after["pending_checkpoint"] is None and state_after["state"] == state_before["state"]
    # 已审批的下游仍可继续:dossier 不需要重新审批
    assert cli.run("dossier", "--project", pid, "--output", str(tmp_path / "out"))["project_status"] == "completed"


# ------------------------------------------------------------------ build-market-forecast


def test_forecast_monthly_cagr_is_labelled_forecast_with_assumptions(cli, pid) -> None:
    out = cli.run("build-market-forecast", "--project", pid, "--horizon-years", "1",
                  "--low-growth", "0.05", "--high-growth", "0.5")
    assert out["is_forecast"] is True and out["scope"] == "top_sample" and out["base_period"] == "2026-12"
    assert out["scenarios"] == ["high", "low", "mid"] and any("method=cagr" in a for a in out["assumptions"])
    doc = _read(cli.pdir(pid) / "external" / "forecast.json")
    mid = doc["result"]["scenarios"]["mid"]
    assert len(mid) == 12 and mid[0]["period"] == "2027-01"
    assert doc["result"]["is_forecast"] is True and doc["evidence"]["id"].startswith("market_metrics@")
    index = _read(cli.pdir(pid) / "external" / "index.json")
    assert index["files"]["forecast.json"] == sha256((cli.pdir(pid) / "external" / "forecast.json").read_bytes()).hexdigest()


def test_forecast_annual_uses_only_complete_years_and_linear_method(cli, pid) -> None:
    out = cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual",
                  "--method", "linear")
    doc = _read(cli.pdir(pid) / "external" / "forecast.json")
    assert out["base_period"] == "2026" and [p["period"] for p in doc["result"]["scenarios"]["mid"]] == ["2027", "2028"]
    years = {p[0]: p[1] for p in doc["params"]["series"]}
    assert set(years) == {"2025", "2026"}
    # 2025 全年 = sum_{i<12}(150 + 7i) = 12*150 + 7*66
    assert years["2025"] == 12 * 150 + 7 * 66


def test_forecast_annual_needs_two_complete_years(cli, tmp_path, capsys) -> None:
    pid = analyzed_project(cli, months=12)
    assert "至少需要两个" in cli.fail("build-market-forecast", "--project", pid, "--horizon-years", "1",
                                     "--granularity", "annual")["error"]


def test_cross_scope_without_coverage_ratio_is_rejected_and_writes_nothing(cli, pid) -> None:
    before = tree_digest(cli.pdir(pid))
    out = cli.fail("build-market-forecast", "--project", pid, "--horizon-years", "1", "--target-scope", "full_market")
    assert "coverage" in out["error"]
    assert tree_digest(cli.pdir(pid)) == before and not (cli.pdir(pid) / "external").exists()


def test_cross_scope_with_sourced_coverage_scales_and_discloses(cli, pid) -> None:
    out = cli.run("build-market-forecast", "--project", pid, "--horizon-years", "1", "--target-scope", "amazon_channel",
                  "--coverage-ratio", "0.5", "--coverage-source", "vendor estimate 2026")
    assert out["scope"] == "amazon_channel" and out["source_scope"] == "top_sample"
    assert any("coverage_ratio=0.5" in a and "vendor estimate 2026" in a for a in out["assumptions"])
    doc = _read(cli.pdir(pid) / "external" / "forecast.json")
    last = doc["params"]["series"][-1][1]
    assert doc["result"]["base_value"] == pytest.approx(last / 0.5)


@pytest.mark.parametrize("extra,message", [
    (["--target-scope", "full_market", "--coverage-ratio", "0.5"], "coverage ratio needs a source"),
    (["--coverage-ratio", "0.5", "--coverage-source", "x"], "--target-scope"),
    (["--scope", "full_market"], "top_sample"),
])
def test_forecast_argument_errors(cli, pid, extra, message) -> None:
    assert message in cli.fail("build-market-forecast", "--project", pid, "--horizon-years", "1", *extra)["error"]


def test_forecast_requires_analyze_first(cli) -> None:
    pid = cli.run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    assert "market_metrics.json" in cli.fail("build-market-forecast", "--project", pid, "--horizon-years", "1")["error"]


# ------------------------------------------------------------------ build-cross-channel


def test_cross_channel_requires_forecast_and_tiktok_units(cli, pid) -> None:
    assert "forecast" in cli.fail("build-cross-channel", "--project", pid)["error"]
    cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual")
    assert "tiktok-units" in cli.fail("build-cross-channel", "--project", pid)["error"]


def test_cross_channel_facts_only_with_blank_decision_slot(cli, pid) -> None:
    cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual")
    cli.import_sample(pid, "tiktok-units")
    out = cli.run("build-cross-channel", "--project", pid)
    assert out["geo"] == "us" and out["unit"] == "units" and out["rows"] == 3
    doc = _read(cli.pdir(pid) / "external" / "cross_channel.json")
    summary = doc["summary"]
    rows = {r["period"]: r for r in summary["table"]}
    assert set(rows) == {"2026", "2027", "2028"}
    assert rows["2026"]["tiktok_units"] == 144 and rows["2026"]["tiktok_is_forecast"] is True
    assert rows["2027"]["amazon_is_forecast"] is True and rows["2027"]["tiktok_units"] == pytest.approx(172.8)
    assert summary["human_decision_slot"] == "由人工填写"
    blob = json.dumps(summary, ensure_ascii=False)
    assert_no_decision_language([blob])  # 复用库里的禁词守卫(含大小写不敏感的 YES/NO)
    assert not re.search(r"建议|推荐|应当|值得", blob)
    assert any("口径差异" in text for text in summary["limitations"])


def test_cross_channel_ambiguous_geo_segment_needs_explicit_choice(cli, pid) -> None:
    cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual")
    extra = ("geo,segment,channel,period,units,is_forecast,evidence_id\n"
             "global,total,tiktok,2024,10,false,g:t:2024\nglobal,total,all,2024,100,false,g:a:2024\n")
    cli.import_sample(pid, "tiktok-units")
    cli.import_file(pid, "tiktok-units", cli.write("tt2.csv", extra), TIKTOK_META)
    assert "--geo" in cli.fail("build-cross-channel", "--project", pid)["error"]
    assert cli.run("build-cross-channel", "--project", pid, "--geo", "us", "--segment", "countertop")["segment"] == "countertop"
