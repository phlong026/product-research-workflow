from __future__ import annotations

from hashlib import sha256
import json

import pytest

openpyxl = pytest.importorskip("openpyxl")

from research_workflow.classification import ClassificationBatch, ProductClassification
from research_workflow.data.models import BSRRecord
from research_workflow.dossier import (
    ResearchDossierGenerator,
    SourceArtifact,
    write_research_workbook,
)
from research_workflow.dossier import workbook as workbook_module
from tests.test_dossier_t11 import sample_input

EXPECTED_SHEETS = [
    "说明",
    "表1_清洗月度数据",
    "表3_分类结果",
    "市场_月度",
    "市场_地区",
    "市场_分类",
    "市场_分解",
    "市场_跨期",
    "市场_国别分类透视",
    "市场_季节性",
    "竞争_集中度",
    "竞争_卖家集中度",
    "竞争_BSR层级",
    "竞争_头腰尾",
    "竞争_虹吸",
    "竞争_单点名次矩阵",
    "竞争_单点名次变化",
    "新品",
    "新品_留存",
    "产品迭代",
    "数据质量",
    "缺失证据",
    "来源与哈希",
]
NOT_PROVIDED = "本次运行未提供"


def full_input(tmp_path):
    dossier = sample_input(tmp_path)
    dossier.records = [
        BSRRecord(
            "N", "2026-01", 25, brand="N", title="Nugget maker", seller_region="CN",
            node_rank=1, launch_date="2025-06-01",
        ),
        BSRRecord("B", "2026-01", 55, brand="B", title="Bullet maker", seller_region="US", node_rank=2),
    ]
    dossier.classification = ClassificationBatch(
        project_id="p1",
        schema_hash="abc",
        version="v-cls-1",
        rows={
            "N": ProductClassification("N", "nugget", "high", "title has nugget", "llm"),
            "B": ProductClassification("B", "bullet", "high", "human corrected", "human"),
        },
        approved=True,
        human_edits={"B": "bullet"},
    )
    dossier.data_version = "data-v1"
    dossier.data_hash = "deadbeef"
    dossier.approval_history = [
        {"checkpoint": "data_review", "status": "approved", "by": "alice"},
    ]
    dossier.source_artifacts = [
        SourceArtifact(
            "src-1", "seller_sprite.xlsx", "xlsx", "SellerSprite",
            sha256(b"fake").hexdigest(), "fixture", relative_path="raw/seller_sprite.xlsx",
        )
    ]
    return dossier


def sheet_rows(ws):
    return [list(row) for row in ws.iter_rows(values_only=True)]


def test_workbook_has_all_sheets_in_order(tmp_path):
    path = write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == EXPECTED_SHEETS


def test_readme_sheet_has_blank_manual_decision_cells(tmp_path):
    wb = openpyxl.load_workbook(
        write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx", generated_at="2026-02-01")
    )
    ws = wb["说明"]
    flat = {r[0]: r[1] for r in sheet_rows(ws) if r and r[0] is not None}
    assert flat["项目"] == "p1"
    assert flat["产品"] == "Ice Maker"
    assert flat["站点"] == "US"
    assert flat["数据版本"] == "data-v1"
    assert flat["数据哈希"] == "deadbeef"
    assert flat["分类版本"] == "v-cls-1"
    assert flat["生成时间"] == "2026-02-01"
    assert "data_review" in str(flat["审批历史"])
    assert "最终商业决定由人工填写,系统不代填" in "".join(
        str(c) for r in sheet_rows(ws) for c in r if c
    )
    for label in ("最终结论", "决策人", "决策日期", "决策原因"):
        assert label in flat
        assert flat[label] in (None, "")


def test_records_sheet_values_and_numeric_types(tmp_path):
    ws = openpyxl.load_workbook(write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx"))[
        "表1_清洗月度数据"
    ]
    rows = sheet_rows(ws)
    header = rows[0]
    for col in ("ASIN", "品牌", "标题", "月销量", "月份", "节点排名", "清洗后名次", "卖家所属地", "上架日期"):
        assert col in header
    first = dict(zip(header, rows[1]))
    assert first["ASIN"] == "N"
    assert first["月销量"] == 25 and isinstance(first["月销量"], int)
    assert first["节点排名"] == 1 and isinstance(first["节点排名"], int)
    assert first["清洗后名次"] == 2  # N 销量低于 B
    second = dict(zip(header, rows[2]))
    assert second["ASIN"] == "B"
    assert second["清洗后名次"] == 1  # B 销量更高
    assert second["上架日期"] in (None, "")


def test_classification_sheet(tmp_path):
    ws = openpyxl.load_workbook(write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx"))[
        "表3_分类结果"
    ]
    rows = {r[0]: r for r in sheet_rows(ws)[1:]}
    assert rows["N"][1] == "nugget"
    assert rows["N"][2] == "title has nugget"
    assert rows["N"][3] == "否"
    assert rows["B"][3] == "是"


def test_market_sheets_numeric_and_percent_format(tmp_path):
    wb = openpyxl.load_workbook(write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx"))
    ws = wb["市场_月度"]
    rows = sheet_rows(ws)
    header = rows[0]
    assert header[:3] == ["月份", "总销量", "ASIN数"]
    jan26 = next(dict(zip(header, r)) for r in rows[1:] if r[0] == "2026-01")
    assert jan26["总销量"] == 80 and isinstance(jan26["总销量"], int)
    yoy_col = header.index("同比") + 1
    # 2025-01 -> 2026-01: 100 -> 80 = -20%
    jan26_row = next(i for i, r in enumerate(rows) if r[0] == "2026-01") + 1
    cell = ws.cell(jan26_row, yoy_col)
    assert isinstance(cell.value, float)
    assert cell.value == pytest.approx(-0.20)
    assert "%" in cell.number_format
    # 月度表首月无同比 -> 空单元格
    jan25_row = next(i for i, r in enumerate(rows) if r[0] == "2025-01") + 1
    assert ws.cell(jan25_row, yoy_col).value is None

    assert wb["市场_地区"].max_row >= 2
    seg = sheet_rows(wb["市场_分类"])
    assert seg[0][:3] == ["月份", "分类", "销量"]
    assert {r[1] for r in seg[1:]} == {"nugget", "bullet"}


def test_competition_and_new_product_sheets(tmp_path):
    wb = openpyxl.load_workbook(write_research_workbook(full_input(tmp_path), tmp_path / "w.xlsx"))
    conc = sheet_rows(wb["竞争_集中度"])
    assert conc[0][0] == "月份" and len(conc) >= 2
    assert isinstance(conc[1][1], int)
    assert wb["竞争_BSR层级"].max_row >= 2
    assert wb["竞争_头腰尾"].max_row >= 2
    np_rows = sheet_rows(wb["新品"])
    assert np_rows[0][0] == "ASIN"
    assert np_rows[1][0] == "X"


def test_missing_optional_sections_write_not_provided(tmp_path):
    dossier = sample_input(tmp_path)
    dossier.competition_metrics = None
    dossier.new_product_analysis = None
    wb = openpyxl.load_workbook(write_research_workbook(dossier, tmp_path / "w.xlsx"))
    assert wb.sheetnames == EXPECTED_SHEETS
    for name in ("表1_清洗月度数据", "表3_分类结果", "竞争_集中度", "竞争_BSR层级", "竞争_头腰尾", "竞争_虹吸", "新品"):
        assert wb[name]["A1"].value == NOT_PROVIDED, name


def test_quality_missing_and_sources_sheets(tmp_path):
    dossier = full_input(tmp_path)
    dossier.data_quality = [{"label": "缺品牌行数", "value": 3}]
    dossier.missing_evidence = ["缺少广告数据"]
    wb = openpyxl.load_workbook(write_research_workbook(dossier, tmp_path / "w.xlsx"))
    dq = sheet_rows(wb["数据质量"])
    assert dq[1][:2] == ["缺品牌行数", 3]
    assert isinstance(dq[1][1], int)
    assert sheet_rows(wb["缺失证据"])[1][0] == "缺少广告数据"
    src = sheet_rows(wb["来源与哈希"])
    assert src[0][:4] == ["ID", "名称", "相对路径", "SHA256"]
    assert src[1][:4] == ["src-1", "seller_sprite.xlsx", "raw/seller_sprite.xlsx", sha256(b"fake").hexdigest()]


def test_source_relative_path_falls_back_to_name(tmp_path):
    wb = openpyxl.load_workbook(write_research_workbook(sample_input(tmp_path), tmp_path / "w.xlsx"))
    assert sheet_rows(wb["来源与哈希"])[1][2] == "seller_sprite.xlsx"


def test_workbook_is_deterministic(tmp_path):
    dossier = full_input(tmp_path)
    a = write_research_workbook(dossier, tmp_path / "a.xlsx", generated_at="2026-02-01")
    b = write_research_workbook(dossier, tmp_path / "b.xlsx", generated_at="2026-02-01")
    assert a.read_bytes() == b.read_bytes()


def test_generator_includes_workbook_in_manifest(tmp_path):
    root = ResearchDossierGenerator().generate(full_input(tmp_path), tmp_path / "dossier")
    xlsx = root / "research_workbook.xlsx"
    assert xlsx.exists()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["files"] if e["path"] == "research_workbook.xlsx")
    assert entry["sha256"] == sha256(xlsx.read_bytes()).hexdigest()
    assert entry["size"] == xlsx.stat().st_size


def test_generator_can_disable_workbook(tmp_path):
    root = ResearchDossierGenerator().generate(
        full_input(tmp_path), tmp_path / "dossier", include_workbook=False
    )
    assert not (root / "research_workbook.xlsx").exists()


def test_missing_openpyxl_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(workbook_module, "_import_openpyxl", _raise_missing)
    with pytest.raises(workbook_module.WorkbookDependencyError, match="xlsx"):
        write_research_workbook(sample_input(tmp_path), tmp_path / "w.xlsx")


def test_generator_degrades_without_openpyxl(tmp_path, monkeypatch):
    monkeypatch.setattr(workbook_module, "_import_openpyxl", _raise_missing)
    dossier = sample_input(tmp_path)
    root = ResearchDossierGenerator().generate(dossier, tmp_path / "dossier")
    assert not (root / "research_workbook.xlsx").exists()
    payload = json.loads((root / "data.json").read_text(encoding="utf-8"))
    assert any("xlsx" in item for item in payload["missing_evidence"])
    assert dossier.missing_evidence == []  # 入参不被修改
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert all(e["path"] != "research_workbook.xlsx" for e in manifest["files"])


def _raise_missing():
    raise workbook_module.WorkbookDependencyError(
        "缺少 openpyxl;请安装 optional extra: pip install 'product-research-workflow[xlsx]'"
    )


def test_formula_like_text_is_stored_as_text(tmp_path):
    dossier = full_input(tmp_path)
    dossier.records[0].title = "=HYPERLINK(\"http://x\")"
    ws = openpyxl.load_workbook(write_research_workbook(dossier, tmp_path / "w.xlsx"))["表1_清洗月度数据"]
    cell = ws["C2"]
    assert cell.data_type == "s"
    assert cell.value.startswith("=HYPERLINK")
