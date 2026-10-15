"""T2-8:证据驱动的深挖执行(库 + deep-dive 命令 + dossier 接入)。"""
from __future__ import annotations

import json

import pytest

from research_workflow.deep_dive import SUPPORTED_MODULES, Inputs, run_deep_dive
from research_workflow.deep_dive.project import resolve_modules
from tests.external_data import DEMAND_META, KEYWORDS_CSV, VOC_CSV, Cli, analyzed_project


def _decomp(segment: str, share_contribution: float, market_change=-5.0, segment_change=10.0) -> dict:
    return {
        "segment": segment, "base_month": "2025-01", "current_month": "2026-01", "base_market_sales": 1000,
        "current_market_sales": 950, "base_segment_sales": 100, "current_segment_sales": 110,
        "market_change_pct": market_change, "segment_change_pct": segment_change, "share_change_pp": 1.5,
        "total_change_sales": 10.0, "market_contribution_sales": -5.0, "share_contribution_sales": share_contribution,
    }


def _market(**extra) -> dict:
    base = {"market": [], "segments": [], "seller_regions": [], "decompositions": [], "seasonality": {"years": [], "complete_years": []}}
    return {**base, **extra}


def _run(inputs: Inputs, *modules: str):
    return run_deep_dive(inputs, confirmed_route_ids=["r1"], modules=list(modules))


# ------------------------------------------------------------------ 库


def test_segment_dive_orders_by_contribution_and_describes_direction_only() -> None:
    market = _market(decompositions=[_decomp("small", 2.0), _decomp("big", -50.0, segment_change=-20.0),
                                     _decomp("flat", 1.0, market_change=None)])
    section = _run(Inputs(market), "segment_deep_dive").sections[0]
    assert [f.label.split(":")[0] for f in section.facts] == ["big", "small", "flat"]
    text = {f.label.split(":")[0]: f.value for f in section.facts}
    assert "大盘下降而该细分增长" in text["small"] and "缺基期,不判方向" in text["flat"]
    assert section.facts[0].evidence_ids == ("decomp:2025-01:2026-01:big",)
    assert {s.kind for s in section.slots} == {"report-excerpts", "offsite", "cross-category"}
    assert all(s.status == "missing" for s in section.slots)


def test_long_tables_are_capped_and_disclosed() -> None:
    market = _market(decompositions=[_decomp(f"s{i:02d}", float(i)) for i in range(45)])
    section = _run(Inputs(market), "replacement_research").sections[0]
    assert len(section.facts) == 30 and any("共 45 行" in text for text in section.limitations)


def test_seasonality_never_reports_high_low_for_incomplete_years() -> None:
    seasonality = {"years": [
        {"year": 2025, "months_observed": 12, "missing_months": [], "is_complete": True, "high_month": "2025-12", "high_sales": 900,
         "low_month": "2025-02", "low_sales": 100},
        {"year": 2026, "months_observed": 4, "missing_months": ["2026-05"], "is_complete": False},
    ], "complete_years": [2025], "coverage_warning": "only one complete year"}
    section = _run(Inputs(_market(seasonality=seasonality)), "seasonality").sections[0]
    by_id = {f.id: f for f in section.facts}
    assert "高 2025-12(900)" in by_id["season:2025"].value and "不判高低月" in by_id["season:2026"].value
    assert "season:recurring" not in by_id
    assert any("only one complete year" in text for text in section.limitations)


def test_new_products_missing_names_the_reason_and_present_shows_thresholds() -> None:
    missing = _run(Inputs(_market(), context={"new_products_skipped_reason": "未设置推起阈值"}), "new_products").sections[0]
    assert missing.facts == () and missing.slots[0].status == "missing" and "推起阈值" in missing.slots[0].detail

    summary = {"total_products": 10, "successful_products": 4, "launch_success_unknown_products": 2, "retention_rate_pct": 20.0,
               "retention_rate_upper_bound_pct": 30.0, "retention_denominator_note": "n", "promotion_waist_sales_threshold": 200,
               "retention_sales_threshold": 150}
    group = {"dimension": "segment", "value": "nugget", "total_products": 6, "successful_products": 3, "success_rate_pct": 50.0,
             "share_of_successful_pct": 75.0}
    acct = {"account_type": "SC", "total_products": 8, "successful_products": 4, "success_rate_pct": 50.0, "share_of_successful_pct": 100.0}
    data = {"summary": summary, "segments": [group], "seller_regions": [], "account_types": [acct]}
    present = _run(Inputs(_market(), new_products=data), "new_products", "vc_sc_comparison")
    by_module = {s.module: s for s in present.sections}
    facts = {f.id: f for f in by_module["new_products"].facts}
    assert facts["np:success"].value == "4 / 6" and "推起 200 / 留存 150" == facts["np:thresholds"].value
    assert "new_products:segment:nugget" in facts
    assert any(f.id == "new_products:account_type:SC" for f in by_module["vc_sc_comparison"].facts)


def test_external_channels_reports_missing_slots_instead_of_zeros() -> None:
    section = _run(Inputs(_market()), "external_channels").sections[0]
    assert section.facts == () and {s.kind: s.status for s in section.slots}["tiktok-units"] == "missing"
    assert next(s for s in section.slots if s.kind == "cross_channel").detail.startswith("尚未运行")


def test_head_suction_collects_signals_and_external_registers() -> None:
    competition = {"head_suction": [{"month": "2026-03", "head_cutoff": 3, "broad_cutoff": 10, "head_yoy_pct": 12.0,
                                     "broad_yoy_pct": -4.0, "pattern": "head_up_broad_down"}],
                   "concentration": [{"month": "2026-03", "top3_brand_share_pct": 40.0, "top10_brand_share_pct": 70.0}]}
    external = {"big-brand": [{"seq": 1, "record_count": 1, "data": {"brand": "Acme", "product_type": "ice", "status": "candidate_big_brand"}}]}
    section = _run(Inputs(_market(), competition=competition, external=external), "brand_deep_dive").sections[0]
    ids = {f.id for f in section.facts}
    assert "competition:head_suction:2026-03:3:10" in ids and "big_brand:Acme" in ids and "conc:latest" in ids
    assert next(s for s in section.slots if s.kind == "big-brand").status == "present"


def test_demand_modules_cite_registered_scope() -> None:
    evidence = {"source_name": "Magnet", "retrieved_at": "2026-10-01", "scope": "US ice maker"}
    row = {"keyword": "nugget", "latest_search_volume": 600.0, "latest_month": "2026-09", "change_pct": 5.0}
    external = {"keywords": [{"seq": 1, "record_count": 1, "data": {"evidence": evidence, "summary": {"table": [row]}}}]}
    section = _run(Inputs(_market(), external=external), "keyword_research").sections[0]
    assert section.facts[0].evidence_ids == ("external:keywords:1",) and "范围: US ice maker" in section.facts[0].note
    empty = _run(Inputs(_market()), "voc_research", "scenario_research")
    assert all(s.status == "missing" for sec in empty.sections for s in sec.slots)


def test_unknown_module_is_an_error_and_unsupported_is_reported_not_faked() -> None:
    with pytest.raises(ValueError, match="未知深挖模块"):
        _run(Inputs(_market()), "make_money")
    report = _run(Inputs(_market()), "new_product_brands", "manual_research")
    assert report.sections == [] and {u["module"] for u in report.unsupported} == {"new_product_brands", "manual_research"}


def test_aliases_share_one_section_and_every_supported_module_runs_on_empty_input() -> None:
    report = run_deep_dive(Inputs(_market()), confirmed_route_ids=[], modules=list(SUPPORTED_MODULES))
    modules = [s.module for s in report.sections]
    assert len(modules) == len(set(modules)) and "trend_sustainability" in modules and "google_trends" not in modules
    assert report.human_decision_slot  # 人工决策栏永远在,且系统不代填


def test_resolve_modules_expands_only_human_confirmed_routes(tmp_path) -> None:
    plan = {"suggestions": [
        {"route_id": "a", "title": "A", "reason": "r", "evidence_ids": ["x"], "next_modules": ["seasonality", "external_channels"], "priority": "high"},
        {"route_id": "b", "title": "B", "reason": "r", "evidence_ids": [], "next_modules": ["brand_deep_dive"], "priority": "normal"},
    ], "confirmed_route_ids": ["a"], "human_confirmed": False}
    (tmp_path / "route_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert resolve_modules(tmp_path, []) == ([], [])  # 未确认:路线不展开
    plan["human_confirmed"] = True
    (tmp_path / "route_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert resolve_modules(tmp_path, ["seasonality", "voc_research"]) == (["a"], ["seasonality", "external_channels", "voc_research"])


# ------------------------------------------------------------------ CLI / dossier


@pytest.fixture()
def cli(tmp_path, capsys) -> Cli:
    return Cli(tmp_path, capsys)


def test_deep_dive_requires_confirmed_route_or_explicit_modules(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)  # 确认了但一条路线都没选
    err = cli.fail("deep-dive", "--project", pid)
    assert "没有可执行的深挖模块" in err["error"]
    assert not (cli.pdir(pid) / "deep_dive.json").exists()
    err = cli.fail("deep-dive", "--project", pid, "--modules", "nope")
    assert "未知深挖模块" in err["error"]


def test_deep_dive_before_analyze_is_rejected(cli) -> None:
    pid = cli.run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    assert "analyze" in cli.fail("deep-dive", "--project", pid, "--modules", "seasonality")["error"]


def test_deep_dive_end_to_end_into_dossier_and_staleness(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)
    out = cli.run("deep-dive", "--project", pid, "--modules", "seasonality,trend_sustainability,external_channels,new_product_brands")
    assert [s["module"] for s in out["sections"]] == ["seasonality", "trend_sustainability", "external_channels"]
    assert out["unsupported"][0]["module"] == "new_product_brands" and "不含商业结论" in out["note"]
    doc = json.loads((cli.pdir(pid) / "deep_dive.json").read_text(encoding="utf-8"))
    assert doc["inputs"]["market_metrics.json"] and doc["report"]["human_decision_slot"]

    dossier = cli.tmp_path / "d1"
    cli.run("dossier", "--project", pid, "--output", str(dossier))
    report = (dossier / "report.md").read_text(encoding="utf-8")
    assert "## 深挖研究(证据驱动)" in report and "季节性" in report and "深挖模块无法自动执行:new_product_brands" in report
    assert "深挖证据缺失:渠道迁移线索/TikTok 渠道台数" in report
    assert (dossier / "evidence" / "deep_dive.json").is_file()
    assert json.loads((dossier / "data.json").read_text(encoding="utf-8"))["deep_dive"]["report"]["sections"]

    # 新登记证据后,旧深挖过期:dossier 拒绝,重跑后恢复
    cli.import_file(pid, "keywords", cli.write("kw.csv", KEYWORDS_CSV), DEMAND_META)
    err = cli.fail("dossier", "--project", pid, "--output", str(cli.tmp_path / "d2"))
    assert "deep-dive" in err["error"] and "过期" in err["error"]
    cli.run("deep-dive", "--project", pid, "--modules", "keyword_research,voc_research")
    cli.import_file(pid, "voc", cli.write("voc.csv", VOC_CSV), DEMAND_META)  # 又变了一次
    assert "过期" in cli.fail("dossier", "--project", pid, "--output", str(cli.tmp_path / "d3"))["error"]
    cli.run("deep-dive", "--project", pid, "--modules", "keyword_research,voc_research")
    cli.run("dossier", "--project", pid, "--output", str(cli.tmp_path / "d4"))
    assert "nugget ice maker" in (cli.tmp_path / "d4" / "report.md").read_text(encoding="utf-8")


def test_dossier_without_deep_dive_says_it_was_not_run(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)
    cli.run("dossier", "--project", pid, "--output", str(cli.tmp_path / "d"))
    report = (cli.tmp_path / "d" / "report.md").read_text(encoding="utf-8")
    assert "研究路线确认后尚未执行深挖" in report and "本次运行未执行深挖" in report


def test_reanalyze_discards_stale_deep_dive(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)
    cli.run("deep-dive", "--project", pid, "--modules", "seasonality")
    assert (cli.pdir(pid) / "deep_dive.json").is_file()
    cli.run("analyze", "--project", pid)
    assert not (cli.pdir(pid) / "deep_dive.json").exists()
