"""T6:关键词 / 场景 / VOC 外部证据(离线纯库 + import-evidence 接线)。"""
from __future__ import annotations

import json

import pytest

from research_workflow.voc_external import (
    load_keyword_rows,
    load_scenario_rows,
    load_voc_rows,
    summarize_keywords,
    summarize_scenarios,
    summarize_voc,
)
from tests.external_data import (
    DEMAND_META,
    KEYWORDS_CSV,
    SCENARIOS_CSV,
    VOC_CSV,
    Cli,
    analyzed_project,
)


def _write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 库:关键词


def test_keywords_use_latest_observed_month_and_never_treat_missing_as_zero(tmp_path) -> None:
    summary = summarize_keywords(load_keyword_rows(_write(tmp_path, "k.csv", KEYWORDS_CSV)))
    rows = {r["keyword"]: r for r in summary["table"]}
    nugget = rows["nugget ice maker"]
    assert nugget["latest_month"] == "2026-09" and nugget["latest_search_volume"] == 60000
    assert nugget["previous_search_volume"] == 52000 and nugget["change_pct"] == pytest.approx(15.38)
    assert rows["clear ice maker"]["latest_search_volume"] is None  # 缺值不当 0
    assert summary["missing_volume_keywords"] == 1
    # 缺搜索量的词排在最后,且不进份额分母
    assert [r["keyword"] for r in summary["table"]][-1] == "clear ice maker"
    shares = {s["segment"]: s["share_pct"] for s in summary["segment_volume"]}
    assert shares == {"nugget": pytest.approx(66.67), "bullet": pytest.approx(33.33)}


def test_keywords_reject_duplicate_month_bad_month_and_negative(tmp_path) -> None:
    with pytest.raises(ValueError, match="重复"):
        load_keyword_rows(_write(tmp_path, "a.csv", "keyword,search_volume,month\nx,1,2026-01\nX,2,2026-01\n"))
    with pytest.raises(ValueError, match="YYYY-MM"):
        load_keyword_rows(_write(tmp_path, "b.csv", "keyword,search_volume,month\nx,1,2026-13\n"))
    with pytest.raises(ValueError, match="不能为负"):
        load_keyword_rows(_write(tmp_path, "c.csv", "keyword,search_volume\nx,-5\n"))
    with pytest.raises(ValueError, match="不是数字"):
        load_keyword_rows(_write(tmp_path, "d.csv", "keyword,search_volume\nx,abc\n"))


# ------------------------------------------------------------------ 库:场景 / VOC


def test_scenarios_share_excludes_rows_without_counts(tmp_path) -> None:
    summary = summarize_scenarios(load_scenario_rows(_write(tmp_path, "s.csv", SCENARIOS_CSV)))
    table = {r["scenario"]: r for r in summary["table"]}
    assert summary["mentions_total"] == 50
    assert table["camping"]["share_pct"] == 80.0 and table["office"]["share_pct"] == 20.0
    assert table["party"]["missing_count_rows"] == 1 and table["party"]["mentions"] == 0


def test_voc_counts_by_theme_and_validates_sentiment_and_rating(tmp_path) -> None:
    summary = summarize_voc(load_voc_rows(_write(tmp_path, "v.csv", VOC_CSV)))
    noise = next(r for r in summary["table"] if r["theme"] == "noise")
    assert noise["excerpts"] == 2 and noise["negative"] == 2 and noise["negative_share_pct"] == 100.0
    assert summary["sentiment_counts"] == {"positive": 1, "negative": 2, "neutral": 0}
    assert summary["excerpt_count"] == 3 and summary["table"][0]["theme"] == "noise"
    with pytest.raises(ValueError, match="sentiment"):
        load_voc_rows(_write(tmp_path, "b.csv", "theme,sentiment,excerpt\nx,angry,y\n"))
    with pytest.raises(ValueError, match="rating"):
        load_voc_rows(_write(tmp_path, "r.csv", "theme,sentiment,excerpt,rating\nx,正面,y,9\n"))
    chinese = load_voc_rows(_write(tmp_path, "z.csv", "主题,情感,原文\n噪音,负面,太吵了\n"))
    assert chinese[0].sentiment == "negative"


def test_voc_keeps_only_a_few_examples_per_theme(tmp_path) -> None:
    body = "theme,sentiment,excerpt\n" + "".join(f"t,negative,line {i}\n" for i in range(10))
    summary = summarize_voc(load_voc_rows(_write(tmp_path, "many.csv", body)))
    assert summary["table"][0]["excerpts"] == 10 and len(summary["table"][0]["examples"]) == 3


# ------------------------------------------------------------------ 接线


@pytest.fixture()
def cli(tmp_path, capsys) -> Cli:
    return Cli(tmp_path, capsys)


@pytest.mark.parametrize("kind,name,content", [
    ("keywords", "kw.csv", KEYWORDS_CSV), ("scenarios", "sc.csv", SCENARIOS_CSV), ("voc", "voc.csv", VOC_CSV),
])
def test_import_requires_scope_and_registers_evidence(cli, kind, name, content) -> None:
    pid = analyzed_project(cli, months=3)
    path = cli.write(name, content)
    no_scope = [m for m in DEMAND_META if not m.startswith("scope=")]
    err = cli.fail("import-evidence", "--project", pid, "--kind", kind, "--file", str(path),
                   *[x for m in no_scope for x in ("--meta", m)])
    assert "scope" in err["error"]
    assert not (cli.pdir(pid) / "external" / f"{kind}.json").exists()  # 失败不落盘

    out = cli.import_file(pid, kind, path, DEMAND_META)
    assert out["kind"] == kind and "范围" in out["summary"]
    stored = json.loads((cli.pdir(pid) / "external" / f"{kind}.json").read_text(encoding="utf-8"))
    evidence = stored["entries"][0]["data"]["evidence"]
    assert evidence["scope"].startswith("US 站") and evidence["source_name"] == "Helium10 Magnet"
    assert evidence["retrieved_at"] == "2026-10-01"


def test_dossier_includes_demand_tables_and_lists_missing_kinds(cli) -> None:
    pid = analyzed_project(cli, months=3, confirm_route=True)
    out_dir = cli.tmp_path / "dossier_gap"
    cli.run("dossier", "--project", pid, "--output", str(out_dir))
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "外部证据尚未提供:关键词搜索量" in report and "外部证据尚未提供:用户评论(VOC)主题" in report

    for kind, name, content in (("keywords", "kw.csv", KEYWORDS_CSV), ("voc", "voc.csv", VOC_CSV)):
        cli.import_file(pid, kind, cli.write(name, content), DEMAND_META)
    out_dir = cli.tmp_path / "dossier_full"
    cli.run("dossier", "--project", pid, "--output", str(out_dir))
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "外部证据尚未提供:关键词搜索量" not in report
    assert "nugget ice maker" in report and "too loud at night" in report
    assert "取数范围" in report and "外部证据尚未提供:使用场景" in report
