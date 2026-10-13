"""CSV 导出对疑似公式文本加前缀、读回还原(防 Excel 公式注入,且不破坏 导出→修改→上传 的原文)。"""
from research_workflow.data import read_tabular_rows, write_csv_rows


def test_csv_export_neutralizes_formulas_but_roundtrips_exactly(tmp_path):
    rows = [{"title": "=cmd|' /C calc'!A0", "brand": "-X Brand", "sales": -5, "ok": "plain", "at": "@SUM(1)"}]
    path = write_csv_rows(tmp_path / "t.csv", rows)
    raw = path.read_text(encoding="utf-8-sig")
    assert "'=cmd" in raw and "'-X Brand" in raw and "'@SUM" in raw  # 打开 CSV 不会被当公式
    back = read_tabular_rows(path)[0]
    assert back["title"] == "=cmd|' /C calc'!A0" and back["brand"] == "-X Brand"
    assert back["at"] == "@SUM(1)" and back["ok"] == "plain"
    assert back["sales"] == "-5"  # 数值列不加前缀


def test_roundtrip_is_lossless_for_apostrophe_and_formula_lookalikes(tmp_path):
    values = ["'quoted", "'=literal", "''=twice", "=SUM(1)", "  =lead space", "-5 apples", "plain", "'", ""]
    path = write_csv_rows(tmp_path / "t.csv", [{"v": v} for v in values])
    assert [r["v"] for r in read_tabular_rows(path)] == values
