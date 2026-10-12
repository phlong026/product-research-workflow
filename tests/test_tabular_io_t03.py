from research_workflow.data import read_tabular_rows, write_csv_rows, write_json_rows


def test_csv_json_roundtrip(tmp_path):
    rows = [{"ASIN": "A1", "月销量": 10}, {"ASIN": "A2", "月销量": 20}]
    jp = write_json_rows(tmp_path / "rows.json", rows)
    cp = write_csv_rows(tmp_path / "rows.csv", rows)
    assert read_tabular_rows(jp) == rows
    csv_rows = read_tabular_rows(cp)
    assert csv_rows[0]["ASIN"] == "A1"
    assert csv_rows[1]["月销量"] == "20"
