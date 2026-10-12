from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = (ROOT / "fixtures/golden_cases/ice_maker/baseline_metrics.json").read_text(encoding="utf-8")


def test_no_machine_final_decision_field_in_golden_case():
    lowered = BASELINE.lower()
    assert '"final_decision_owner": "human"' in lowered
    assert '"final_decision": "yes"' not in lowered
    assert '"final_decision": "no"' not in lowered
