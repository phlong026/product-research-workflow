import csv
import json
import pytest

from research_workflow.dossier import ResearchDossierGenerator
from test_dossier_t11 import sample_input


def test_existing_export_is_not_silently_mixed_or_overwritten(tmp_path):
    generator = ResearchDossierGenerator()
    root = generator.generate(sample_input(tmp_path), tmp_path / 'out')
    before = (root / 'manifest.json').read_bytes()
    changed = sample_input(tmp_path)
    changed.new_product_analysis = None
    with pytest.raises(ValueError):
        generator.generate(changed, root)
    assert (root / 'manifest.json').read_bytes() == before


def test_csv_spreadsheet_formula_is_escaped_but_numeric_values_preserved(tmp_path):
    path = tmp_path / 'x.csv'
    ResearchDossierGenerator._write_dataclass_csv(path, [{'label':'=1+1','sales':-1}, {'label':' \t@SUM(1,1)','sales':2}])
    rows = list(csv.DictReader(path.open(encoding='utf-8-sig')))
    assert rows[0]['label'].startswith("'")
    assert rows[1]['label'].startswith("'")
    assert rows[0]['sales'] == '-1'


def test_dashboard_is_offline_without_remote_script(tmp_path):
    root = ResearchDossierGenerator().generate(sample_input(tmp_path), tmp_path / 'out')
    html = (root / 'dashboard.html').read_text()
    assert '<script src=' not in html
    assert '<svg' in html
