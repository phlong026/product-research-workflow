"""dossier 接入外部证据:报告章节、Excel 工作表、manifest、缺失清单与篡改拒绝。"""
from __future__ import annotations

import json
import re
from hashlib import sha256

import pytest

from research_workflow import external_ingest
from research_workflow.competition_external.ledger import assert_no_decision_language
from research_workflow.dossier import ResearchDossierGenerator
from research_workflow.dossier.external import FORECAST_TAG, KIND_LABELS, TITLE, external_gaps
from tests.external_data import ALL_KINDS, Cli, QuietCli, analyzed_project

openpyxl = pytest.importorskip("openpyxl")

NOT_PROVIDED = "本次运行未提供"
SHEETS = ["外部_趋势", "外部_研报摘录", "外部_TikTok台数", "外部_TikTokCAGR", "外部_TikTok预测", "外部_市场预测",
          "外部_跨渠道", "外部_专利检索", "外部_专利记录", "外部_广告", "外部_大品牌", "外部_站外", "外部_跨类目"]


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """全部 9 类外部证据 + 预测 + 跨渠道整合 -> dossier。"""
    cli = QuietCli(tmp_path_factory.mktemp("full"))
    pid = analyzed_project(cli, confirm_route=True)
    for kind in ALL_KINDS:
        cli.import_sample(pid, kind)
    cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual",
            "--low-growth", "0.05", "--high-growth", "0.5")
    cli.run("build-cross-channel", "--project", pid)
    out = cli.run("dossier", "--project", pid, "--output", str(cli.tmp_path / "dossier"))
    return cli, pid, cli.tmp_path / "dossier", out


def _section(report: str) -> str:
    return report[report.index(f"## {TITLE}"): report.index("## 8. 人工最终判断")]


# ------------------------------------------------------------------ 报告


def test_report_has_external_section_with_every_part(full) -> None:
    _, _, dossier, out = full
    assert "report.md" in out["files"]
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    for title in ("Google Trends", "研报摘录", "TikTok 渠道台数与份额", "TikTok 台数 CAGR(2024-2026)",
                  "TikTok 台数预测(2027-2028 外推)", "市场规模预测(基于本系统 Top 样本)",
                  "跨渠道事实整合(Amazon 与 TikTok,同单位:台)", "专利检索日志", "专利记录",
                  "广告 / 流量结构 / 留评率 / CPC", "大品牌核查", "站外证据", "同品牌跨类目 ASIN"):
        assert f"### {title}" in section, title


def test_every_number_shows_source_date_and_evidence_id(full) -> None:
    _, _, dossier, _ = full
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    assert "trends:US:ice maker:2026-01" in section and "Google Trends" in section and "2026-03-01" in section
    assert "tt:tiktok:2026" in section and "Vendor Report" in section and "2026-02-01" in section
    assert "market_metrics@" in section and "rep:1" in section and "Statista" in section


def test_forecasts_are_labelled_with_assumptions(full) -> None:
    _, _, dossier, _ = full
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    forecast = section[section.index("### 市场规模预测"): section.index("### 跨渠道事实整合")]
    projection = section[section.index("### TikTok 台数预测"): section.index("### 市场规模预测")]
    for part in (forecast, projection):
        rows = [line for line in part.splitlines() if line.startswith("| ") and ("| 低 |" in line or "| 中 |" in line or "| 高 |" in line)]
        assert rows and all(FORECAST_TAG in line for line in rows)
        assert "假设" in part
    assert re.search(r"\| 基期 \|.*观测/声明值", forecast)  # 基期是观测值,不贴预测标签


def test_patents_distinguish_unsearched_from_none_and_carry_disclaimer(full) -> None:
    _, _, dossier, _ = full
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    logs = section[section.index("### 专利检索日志"): section.index("### 专利记录")]
    assert "已检索-范围内未发现" in logs and "| 未检索 |" in logs and "已检索-发现记录" in logs
    assert logs.count("不构成法律意见") >= 1 and "不构成法律意见" in section[section.index("### 专利记录"):]
    assert "未检索" in section[: section.index("### Google Trends")]  # 缺口清单里醒目列出


def test_missing_and_inaccessible_are_listed_prominently(full) -> None:
    _, _, dossier, _ = full
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    head = section[: section.index("### Google Trends")]
    assert "**研报摘录 rep:2 来源不可访问" in head and "**专利检索未执行" in head and "**站外检索未执行" in head
    assert "来源不可访问(inaccessible)" in section


def test_cross_channel_and_big_brand_leave_decision_slots_to_humans(full) -> None:
    _, _, dossier, _ = full
    section = _section((dossier / "report.md").read_text(encoding="utf-8"))
    assert "人工决策栏:由人工填写" in section and "| 由人工填写 |" in section
    assert "仅按 ABA 阈值规则" in section and "口径差异" in section


def test_external_section_has_no_business_decision_wording(full) -> None:
    _, _, dossier, _ = full
    report = (dossier / "report.md").read_text(encoding="utf-8")
    html = (dossier / "dashboard.html").read_text(encoding="utf-8")
    section = _section(report)
    for text in (report[: report.index("## 8. 人工最终判断")], html):  # 第 8 节的人工留空栏是既有内容
        assert not re.search(r"\b(YES|NO)\b", text) and "建议进入" not in text
    assert_no_decision_language(section.splitlines())  # 库里的禁词守卫(大小写不敏感,含侵权等)
    assert TITLE in html and FORECAST_TAG in html


# ------------------------------------------------------------------ Excel


def test_workbook_has_external_sheets_with_numeric_cells(full) -> None:
    _, _, dossier, _ = full
    wb = openpyxl.load_workbook(dossier / "research_workbook.xlsx")
    assert all(name in wb.sheetnames for name in SHEETS)
    forecast = wb["外部_市场预测"]
    header = [c.value for c in forecast[1]]
    value_col = header.index("数值(台)") + 1
    values = [forecast.cell(row=r, column=value_col).value for r in range(2, 9)]
    assert all(isinstance(v, (int, float)) for v in values)  # 数值保持数值类型
    assert FORECAST_TAG in [forecast.cell(row=r, column=header.index("性质") + 1).value for r in range(2, 9)]
    tiktok = wb["外部_TikTok台数"]
    share_col = [c.value for c in tiktok[1]].index("TikTok 份额") + 1
    share = tiktok.cell(row=2, column=share_col)
    assert share.value == pytest.approx(0.10) and share.number_format == "0.00%"
    trends = wb["外部_趋势"]
    assert [trends.cell(row=r, column=1).value for r in range(2, 16)] == ["ice maker"] * 14  # 完整序列在 Excel(报告内只放精简行)
    assert isinstance(trends.cell(row=2, column=3).value, (int, float))


def test_unprovided_kinds_write_not_provided_in_a1(tmp_path, capsys) -> None:
    cli = Cli(tmp_path, capsys)
    pid = analyzed_project(cli, confirm_route=True)
    cli.import_sample(pid, "trends")
    cli.run("dossier", "--project", pid, "--output", str(tmp_path / "out"))
    wb = openpyxl.load_workbook(tmp_path / "out" / "research_workbook.xlsx")
    assert wb["外部_趋势"]["A1"].value != NOT_PROVIDED
    for name in SHEETS:
        if name != "外部_趋势":
            assert wb[name]["A1"].value == NOT_PROVIDED, name


# ------------------------------------------------------------------ manifest 与缺失清单


def test_manifest_lists_external_files_with_correct_hashes(full) -> None:
    cli, pid, dossier, _ = full
    manifest = {f["path"]: f for f in json.loads((dossier / "manifest.json").read_text(encoding="utf-8"))["files"]}
    for kind in ALL_KINDS:
        assert f"evidence/external_{kind}.json" in manifest, kind
    for name in ("index", "forecast", "cross_channel"):
        assert f"evidence/external_{name}.json" in manifest
    for path, entry in manifest.items():
        assert sha256((dossier / path).read_bytes()).hexdigest() == entry["sha256"], path
    index = json.loads((cli.pdir(pid) / "external" / "index.json").read_text(encoding="utf-8"))
    for row in index["entries"]:  # 导入文件的原始快照也进 evidence/ 并有哈希
        snapshot = f"evidence/{row['sha256']}{'.json' if row['file_name'].endswith('.json') else '.csv'}"
        assert manifest[snapshot]["sha256"] == row["sha256"]


def test_data_json_carries_external_payload(full) -> None:
    _, _, dossier, _ = full
    data = json.loads((dossier / "data.json").read_text(encoding="utf-8"))
    assert set(ALL_KINDS) <= set(data["external"]) and data["external"]["forecast"]["result"]["is_forecast"] is True
    assert data["external"]["cross_channel"]["summary"]["human_decision_slot"] == "由人工填写"


def test_missing_evidence_reports_each_unprovided_category(tmp_path, capsys) -> None:
    cli = Cli(tmp_path, capsys)
    pid = analyzed_project(cli, confirm_route=True)
    cli.import_sample(pid, "trends")
    cli.import_sample(pid, "report-excerpts")
    cli.run("dossier", "--project", pid, "--output", str(tmp_path / "out"))
    data = json.loads((tmp_path / "out" / "data.json").read_text(encoding="utf-8"))
    missing = "\n".join(data["missing_evidence"])
    for kind, label in KIND_LABELS.items():
        expected = f"外部证据尚未提供:{label}"
        assert (expected in missing) == (kind not in ("trends", "report-excerpts")), kind
    assert "尚未提供:市场规模预测" in missing and "尚未提供:跨渠道事实整合" in missing
    assert "rep:2 来源不可访问" in missing
    assert "尚未提供" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def test_dossier_without_any_external_still_lists_every_gap(tmp_path, capsys) -> None:
    cli = Cli(tmp_path, capsys)
    pid = analyzed_project(cli, confirm_route=True)
    cli.run("dossier", "--project", pid, "--output", str(tmp_path / "out"))
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    section = _section(report)
    assert section.count("外部证据尚未提供") == len(KIND_LABELS) + 2
    assert NOT_PROVIDED in section
    assert not list((tmp_path / "out" / "evidence").glob("external_*"))


def test_external_none_keeps_legacy_output_unchanged(tmp_path) -> None:
    from tests.test_dossier_t11 import sample_input

    dossier = sample_input(tmp_path)
    assert dossier.external is None
    root = ResearchDossierGenerator().generate(dossier, tmp_path / "legacy")
    assert TITLE not in (root / "report.md").read_text(encoding="utf-8")
    assert "external" not in json.loads((root / "data.json").read_text(encoding="utf-8"))
    wb = openpyxl.load_workbook(root / "research_workbook.xlsx")
    assert not [name for name in wb.sheetnames if name.startswith("外部_")]


def test_gaps_helper_flags_unusable_evidence() -> None:
    ext = external_ingest.empty_external()
    ext["tiktok_analysis"] = [{"geo": "us", "segment": "total", "missing_months_2026": ["2026-03"],
                               "cagr_2024_2026": {"status": "insufficient_evidence", "missing_reason": "missing 2026 value"}}]
    gaps = external_gaps(ext)
    assert "TikTok us/total 缺失 2026 月份:2026-03" in gaps and any("CAGR 证据不足" in g for g in gaps)


# ------------------------------------------------------------------ 篡改与过期


def _prepared(tmp_path, capsys):
    cli = Cli(tmp_path, capsys)
    pid = analyzed_project(cli, confirm_route=True)
    cli.import_sample(pid, "trends")
    cli.import_sample(pid, "tiktok-units")
    return cli, pid


def _assert_blocked(cli, pid, tmp_path, message):
    out = cli.fail("dossier", "--project", pid, "--output", str(tmp_path / "out"))
    assert message in out["error"]
    assert not (tmp_path / "out").exists()
    assert cli.run("status", "--project", pid)["project"]["status"] != "completed"


def test_tampered_external_result_file_blocks_export(tmp_path, capsys) -> None:
    cli, pid = _prepared(tmp_path, capsys)
    path = cli.pdir(pid) / "external" / "trends.json"
    path.write_text(path.read_text(encoding="utf-8").replace("Google Trends", "Fabricated Source"), encoding="utf-8")
    _assert_blocked(cli, pid, tmp_path, "外部证据文件被修改或缺失: external/trends.json")


def test_tampered_raw_snapshot_blocks_export(tmp_path, capsys) -> None:
    cli, pid = _prepared(tmp_path, capsys)
    row = json.loads((cli.pdir(pid) / "external" / "index.json").read_text(encoding="utf-8"))["entries"][0]
    (cli.pdir(pid) / row["snapshot"]).write_text("date,keyword,value\n2025-01-01,ice maker,99\n", encoding="utf-8")
    _assert_blocked(cli, pid, tmp_path, "快照缺失或哈希不匹配")


def test_unregistered_or_missing_index_blocks_export(tmp_path, capsys) -> None:
    cli, pid = _prepared(tmp_path, capsys)
    (cli.pdir(pid) / "external" / "sneaky.json").write_text("{}", encoding="utf-8")
    _assert_blocked(cli, pid, tmp_path, "未登记在 index.json")
    (cli.pdir(pid) / "external" / "sneaky.json").unlink()
    (cli.pdir(pid) / "external" / "index.json").unlink()
    _assert_blocked(cli, pid, tmp_path, "缺少 index.json")


def test_stale_forecast_and_cross_channel_are_refused(tmp_path, capsys) -> None:
    cli = Cli(tmp_path, capsys)
    pid = analyzed_project(cli)
    cli.run("build-market-forecast", "--project", pid, "--horizon-years", "2", "--granularity", "annual")
    cli.import_sample(pid, "tiktok-units")
    cli.run("build-cross-channel", "--project", pid)
    pdir = cli.pdir(pid)
    assert external_ingest.load_external(pdir)["cross_channel"]["summary"]["table"]

    cli.import_file(pid, "tiktok-units", cli.write("tt_more.csv", "geo,segment,channel,period,units,is_forecast,evidence_id\n"
                                                    "us,countertop,tiktok,2025,120,false,tt:tiktok:2025\n"),
                    ["source_name=Vendor Report", "source_url=file://v2.csv", "as_of=2026-03-01"])
    with pytest.raises(ValueError, match="cross_channel.json 基于旧的预测或台数数据"):
        external_ingest.load_external(pdir)
    cli.run("build-cross-channel", "--project", pid)  # 重建后恢复
    assert external_ingest.load_external(pdir)["cross_channel"]

    metrics = pdir / "market_metrics.json"
    metrics.write_text(metrics.read_text(encoding="utf-8").replace('"asin_count"', '"asin_count" ', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="forecast.json 基于旧的 market_metrics.json"):
        external_ingest.load_external(pdir)
