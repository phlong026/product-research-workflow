"""流G:dossier 展示分析模块的新字段(纯格式化,不做新算数),合成小数据。"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

openpyxl = pytest.importorskip("openpyxl")

from research_workflow.competition import build_competition_metrics
from research_workflow.data.models import BSRRecord
from research_workflow.dossier import ResearchDossierGenerator, ResearchDossierInput, write_research_workbook
from research_workflow.metrics import IterationConfig, build_iteration_comparison, build_market_metrics
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products

NOT_PROVIDED = "本次运行未提供"
MONTHS_2025 = [f"2025-{m:02d}" for m in range(1, 13)]
CLASSIFICATION = {"A": "nugget", "B": "bullet", "C": "bullet"}


def _records():
    rows = []
    for index, month in enumerate(MONTHS_2025 + ["2026-01"]):
        rows.append(BSRRecord("A", month, 100 + 10 * index, brand="BA", seller_region="CN",
                              seller_id="S1", node_rank=1, launch_date="2024-06-01"))
        rows.append(BSRRecord("B", month, 50, brand="BB", seller_region="US",
                              seller_id="S2", node_rank=2, launch_date="2025-03-01"))
    rows.append(BSRRecord("C", "2026-01", 30, brand="BC", seller_region="US", node_rank=3))  # 无 seller_id
    return rows


def _new_products():
    rows = []
    for month, sales in (("2026-01", 300), ("2026-02", 300), ("2026-03", 300), ("2026-04", 300)):
        rows.append(NewProductSnapshot("P1", month, sales, account_type="VC"))
    for month in ("2026-01", "2026-02", "2026-03"):  # P4:历史达标,当期(2026-04)未观测 => 留存未知
        rows.append(NewProductSnapshot("P4", month, 300))
    for month, sales in (("2026-01", 300), ("2026-02", 300), ("2026-03", 300), ("2026-04", 100)):
        rows.append(NewProductSnapshot("P2", month, sales))
    rows.append(NewProductSnapshot("P3", "2026-04", 10))
    return rows


def make_input(*, competition=True, new_products=True, iteration=True) -> ResearchDossierInput:
    records = _records()
    market = build_market_metrics(records, CLASSIFICATION)
    return ResearchDossierInput(
        project_id="pg",
        product="Ice Maker",
        marketplace="US",
        market_metrics=market,
        competition_metrics=build_competition_metrics(records, rank_cutoffs=(1, 2), head_cutoff=1, broad_cutoff=2)
        if competition else None,
        new_product_analysis=analyze_new_products(_new_products(), NewProductConfig(250, 3, 3, study_month="2026-04"))
        if new_products else None,
        iteration=build_iteration_comparison(records, CLASSIFICATION, config=IterationConfig(as_of=date(2026, 6, 1)))
        if iteration else None,
    )


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = ResearchDossierGenerator().generate(make_input(), tmp_path_factory.mktemp("g") / "dossier")
    return root


def rows_of(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


# ------------------------------------------------------------------ model


def test_iteration_field_defaults_to_none_for_backward_compatibility():
    base = make_input(iteration=False)
    assert base.iteration is None


# ------------------------------------------------------------------ report


def test_report_scope_section_is_at_top_and_lists_all_bases(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    assert report.index("## 口径说明") < report.index("## 0. 数据完整度与待补资料")
    scope = report[report.index("## 口径说明"): report.index("## 0.")]
    for text in ("ASIN 口径", "品牌口径", "卖家口径", "seller_id", "basis=point", "basis=cumulative",
                 "历史成功率", "当期存活率", "原文口径留存率", "预测", "全市场结论",
                 "12 个月齐全", "缺月不当 0", "earliest_launch_date", "earliest_observed_in_data"):
        assert text in scope, text


def test_new_content_has_no_business_decision_wording(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    generated = report[: report.index("## 8. 人工最终判断")]  # 第 8 节的人工留空栏是既有内容
    html = (bundle / "dashboard.html").read_text(encoding="utf-8")
    for text in (generated, html):
        assert not re.search(r"\b(YES|NO)\b", text)
        assert "建议进入" not in text


def test_report_shows_three_concentration_bases_side_by_side(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    block = report[report.index("### 集中度三种口径并列"):]
    block = block[: block.index("\n\n###")] if "\n\n###" in block else block
    header = block.splitlines()[4]
    for label in ("Top3 ASIN口径", "Top3 品牌口径", "Top3 卖家口径", "未知卖家销量占比"):
        assert label in header, label
    # 2026-01: A=130+... 卖家口径 S1/S2 + 未知卖家 C(30)
    assert "| 2026-01 |" in block
    assert "basis=cumulative" in report  # BSR 层级标注累计口径


def test_report_point_rank_matrix_marks_basis_point_and_blanks(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    assert "### 单点名次逐月销量矩阵（basis=point）" in report
    table = report[report.index("### 单点名次逐月销量矩阵"):]
    header = table.splitlines()[4]
    assert "| 1名 | 2名 | 3名 |" in header and "100名" in header
    jan = next(line for line in table.splitlines() if line.startswith("| 2026-01 |"))
    assert "| 30 |" in jan or "| 30 " in jan  # 3 名 = C
    assert " - " in jan  # 不足的名次为空,不是 0
    assert "单点名次变化（basis=point" in report


def test_report_region_segment_seasonality_and_region_change(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    assert "### 国别 × 分类透视（2026-01）" in report
    assert "占已知地区销量" in report
    assert "### 季节性（高低月）" in report
    season = report[report.index("### 季节性（高低月）"):]
    assert "| 2025 | 12 | 是 |" in season
    assert "2025-01" in season and "2025-12" in season  # 低月/高月
    assert "| 2026 | 1 | 否 |" in season
    assert "incomplete_year:1/12_months_observed" in season
    assert "待验证假设" in season
    assert "### 卖家地区环比/同比（2026-01）" in report
    assert "覆盖提示" in report


def test_report_retention_triple_is_side_by_side_and_labelled_different(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    section = report[report.index("### 历史成功率 / 当期存活率 / 原文口径留存率"):]
    section = section[: section.index("\n## 6.")]
    assert "三者口径不同，不得混用" in section
    for label in ("历史成功率", "当期存活率", "原文口径留存率(下限)", "原文口径留存率(上限)",
                  "留存率口径说明", "新品成功阈值(月销量)", "留存阈值(月销量)",
                  "早期窗口完整新品数", "早期窗口达标比例"):
        assert f"| {label} |" in section, label
    assert "分母=全部有效新品(4)" in section  # P1..P4
    assert "分母=当期已观测的成功新品" in section


def test_report_iteration_section_distinguishes_launch_from_observed(bundle):
    report = (bundle / "report.md").read_text(encoding="utf-8")
    section = report[report.index("### 产品迭代进程"):]
    section = section[: section.index("## 3.")]
    assert "earliest_launch_date（上架日期）" in section
    assert "earliest_observed_in_data（数据首次出现月，非上架日期）" in section
    assert "2024-06-01" in section and "2025-01" in section


def test_report_marks_iteration_not_provided_when_absent(tmp_path):
    root = ResearchDossierGenerator().generate(make_input(iteration=False), tmp_path / "d")
    report = (root / "report.md").read_text(encoding="utf-8")
    section = report[report.index("### 产品迭代进程"):]
    assert NOT_PROVIDED in section[: section.index("## 3.")]


def test_dashboard_has_scope_and_new_tables(bundle):
    html = (bundle / "dashboard.html").read_text(encoding="utf-8")
    for text in ("口径说明", "集中度三种口径并列", "单点名次逐月销量矩阵", "国别 × 分类透视",
                 "季节性", "原文口径留存率", "产品迭代进程"):
        assert text in html, text
    assert 'id="chart-data"' in html


def test_data_json_carries_iteration(bundle, tmp_path):
    payload = json.loads((bundle / "data.json").read_text(encoding="utf-8"))
    assert payload["iteration"]["segments"][0]["earliest_launch_date"] == "2024-06-01"
    root = ResearchDossierGenerator().generate(make_input(iteration=False), tmp_path / "d2")
    assert json.loads((root / "data.json").read_text(encoding="utf-8"))["iteration"] is None


# ------------------------------------------------------------------ csv


def test_csv_directory_has_new_files(bundle):
    names = {p.name for p in (bundle / "csv").iterdir()}
    assert {"market_region_segments.csv", "market_seasonality.csv", "point_rank_matrix.csv",
            "point_rank_changes.csv", "new_product_summary.csv", "product_iteration.csv"} <= names
    concentration = (bundle / "csv" / "concentration.csv").read_text(encoding="utf-8-sig")
    assert "top3_seller_share_pct" in concentration.splitlines()[0]
    matrix = (bundle / "csv" / "point_rank_matrix.csv").read_text(encoding="utf-8-sig").splitlines()
    assert matrix[0].startswith("month,ordering_basis,basis,rank_1,rank_2,rank_3")
    summary = (bundle / "csv" / "new_product_summary.csv").read_text(encoding="utf-8-sig")
    assert "retention_rate_upper_bound_pct" in summary


# ------------------------------------------------------------------ workbook


def test_workbook_new_sheets_present(bundle):
    wb = openpyxl.load_workbook(bundle / "research_workbook.xlsx")
    for name in ("市场_国别分类透视", "市场_季节性", "竞争_卖家集中度", "竞争_单点名次矩阵",
                 "竞争_单点名次变化", "新品_留存", "产品迭代"):
        assert name in wb.sheetnames, name


def test_workbook_region_segment_values_are_numeric(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["市场_国别分类透视"]
    rows = rows_of(ws)
    header = rows[0]
    cell = next(dict(zip(header, r)) for r in rows[1:] if r[:3] == ["2026-01", "US", "bullet"])
    assert cell["销量"] == 80 and isinstance(cell["销量"], int)  # B=50 + C=30
    assert cell["ASIN数"] == 2
    assert isinstance(cell["占当月销量(含未知地区)"], float)
    assert cell["占当月销量(含未知地区)"] == pytest.approx(80 / 300)


def test_workbook_seasonality_sheet(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["市场_季节性"]
    rows = rows_of(ws)
    by_year = {r[0]: dict(zip(rows[0], r)) for r in rows[1:3]}
    assert by_year[2025]["12月齐全"] is True
    assert by_year[2025]["高月"] == "2025-12" and by_year[2025]["低月"] == "2025-01"
    assert isinstance(by_year[2025]["高月销量"], int)
    assert by_year[2026]["12月齐全"] is False
    assert by_year[2026]["高月"] is None and by_year[2026]["高月销量"] is None
    assert "incomplete_year" in by_year[2026]["覆盖提示"]
    assert any("12 个月齐全" in str(c) for r in rows[3:] for c in r if c)


def test_workbook_seller_concentration_three_bases_and_unknown(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["竞争_卖家集中度"]
    rows = rows_of(ws)
    header = rows[0]
    for label in ("Top3 ASIN占比[口径=ASIN]", "Top3 品牌占比[口径=品牌]", "Top3 卖家占比[口径=seller_id]"):
        assert label in header
    last = dict(zip(header, rows[-1]))
    assert last["月份"] == "2026-01"
    assert last["未知卖家销量(缺seller_id)"] == 30 and isinstance(last["未知卖家销量(缺seller_id)"], int)
    assert last["未知卖家ASIN数"] == 1
    assert last["未知卖家销量占比"] == pytest.approx(30 / 300)
    assert last["Top3 卖家占比[口径=seller_id]"] == pytest.approx(1.0)


def test_workbook_cumulative_sheets_carry_basis_column(bundle):
    wb = openpyxl.load_workbook(bundle / "research_workbook.xlsx")
    for name in ("竞争_BSR层级", "竞争_头腰尾", "竞争_虹吸"):
        rows = rows_of(wb[name])
        assert "口径(basis)" in rows[0], name
        assert rows[1][rows[0].index("口径(basis)")] == "cumulative"


def test_workbook_point_rank_matrix_rows_are_months_columns_are_ranks(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["竞争_单点名次矩阵"]
    rows = rows_of(ws)
    header = rows[0]
    assert header[:3] == ["月份", "排序口径", "口径(basis)"]
    assert header[3:] == [f"{r}名" for r in (1, 2, 3, 4, 5, 10, 20, 30, 50, 70, 100)]
    jan26 = dict(zip(header, next(r for r in rows[1:] if r[0] == "2026-01")))
    assert jan26["口径(basis)"] == "point"
    assert jan26["1名"] == 220 and isinstance(jan26["1名"], int)  # A: 100 + 10*12
    assert jan26["2名"] == 50 and jan26["3名"] == 30
    assert jan26["4名"] is None and jan26["100名"] is None  # 不足为空,非 0
    jan25 = dict(zip(header, next(r for r in rows[1:] if r[0] == "2025-01")))
    assert jan25["3名"] is None


def test_workbook_point_rank_changes_sheet(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["竞争_单点名次变化"]
    rows = rows_of(ws)
    header = rows[0]
    first = dict(zip(header, rows[1]))
    assert first["口径(basis)"] == "point"
    assert {"名次", "基准月", "当前月", "绝对变化", "相对变化", "方向"} <= set(header)


def test_workbook_retention_sheet_values(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["新品_留存"]
    rows = rows_of(ws)
    assert rows[0] == ["指标", "值", "分母与口径"]
    table = {r[0]: r for r in rows[1:]}
    for label in ("历史成功率", "当期存活率", "原文口径留存率(下限)", "原文口径留存率(上限)", "留存率口径说明",
                  "新品成功阈值(月销量)", "留存阈值(月销量)", "早期窗口达标比例"):
        assert label in table, label
    assert table["历史成功率"][1] == pytest.approx(3 / 4)  # P1, P2, P4
    assert table["原文口径留存率(下限)"][1] == pytest.approx(1 / 4)  # 仅 P1;P4 当期未观测不计入
    assert table["原文口径留存率(上限)"][1] == pytest.approx(2 / 4)  # P4 未知计入上限
    assert table["当期存活率"][1] == pytest.approx(1 / 2)  # 已观测成功新品 P1,P2 中 P1 存活
    assert isinstance(table["新品成功阈值(月销量)"][1], int)
    assert "分母=全部有效新品" in table["历史成功率"][2]


def test_workbook_iteration_sheet_separates_launch_and_observed(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["产品迭代"]
    rows = rows_of(ws)
    header = rows[0]
    assert "earliest_launch_date(上架日期)" in header
    assert "earliest_observed_in_data(数据首次出现月,非上架日期)" in header
    first = dict(zip(header, rows[1]))
    assert first["顺序"] == 1 and isinstance(first["顺序"], int)
    assert first["earliest_launch_date(上架日期)"] == "2024-06-01"
    assert first["earliest_observed_in_data(数据首次出现月,非上架日期)"] == "2025-01"
    assert any("NOT a launch date" in str(c) for r in rows for c in r if c)


def test_workbook_region_sheet_has_mom_yoy_columns(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["市场_地区"]
    rows = rows_of(ws)
    header = rows[0]
    for label in ("销量环比", "销量同比", "份额环比(pp)", "份额同比(pp)", "覆盖提示", "待验证假设"):
        assert label in header, label
    jan26_us = next(dict(zip(header, r)) for r in rows[1:] if r[:2] == ["2026-01", "US"])
    assert isinstance(jan26_us["销量同比"], float)  # 2025-01 基期存在


def test_workbook_missing_inputs_write_not_provided(tmp_path):
    path = write_research_workbook(
        make_input(competition=False, new_products=False, iteration=False), tmp_path / "w.xlsx"
    )
    wb = openpyxl.load_workbook(path)
    for name in ("竞争_卖家集中度", "竞争_单点名次矩阵", "竞争_单点名次变化", "新品_留存", "产品迭代"):
        assert wb[name]["A1"].value == NOT_PROVIDED, name
    assert wb["市场_季节性"]["A1"].value == "年份"  # 市场指标始终存在


def test_workbook_readme_sheet_carries_scope_notes_and_blank_decision(bundle):
    ws = openpyxl.load_workbook(bundle / "research_workbook.xlsx")["说明"]
    rows = rows_of(ws)
    flat = {r[0]: r[1] for r in rows if r and r[0] is not None}
    assert flat["最终结论"] in (None, "")
    scope_text = " ".join(str(r[1]) for r in rows if r[0] and str(r[0]).startswith("口径"))
    assert "卖家口径" in scope_text and "缺月不当 0" in scope_text
    assert not re.search(r"\b(YES|NO)\b", scope_text)
