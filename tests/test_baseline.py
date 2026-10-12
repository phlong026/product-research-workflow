import json
from pathlib import Path

from research_workflow.golden_case import load_baseline

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "fixtures/golden_cases/ice_maker/baseline_metrics.json"


def test_golden_case_is_human_decision_owned():
    baseline = load_baseline(BASELINE)
    assert baseline.final_decision_owner == "human"


def test_new_product_success_rate_is_internally_consistent():
    baseline = load_baseline(BASELINE)
    total = baseline.metrics["new_products_total"].value
    success = baseline.metrics["new_products_success"].value
    expected_rate = baseline.metrics["new_product_success_rate_pct"]
    actual_rate = success / total * 100
    assert expected_rate.matches(actual_rate)


def test_core_regression_metrics_are_present():
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    required = {
        "market_yoy_2026_jan_apr_pct",
        "nugget_yoy_2026_jan_apr_pct",
        "nugget_share_change_pp",
        "top3_asin_share_pct",
        "top10_asin_share_pct",
        "top3_brand_share_pct",
        "top10_brand_share_pct",
        "new_products_total",
        "new_products_success",
        "new_product_success_rate_pct",
        "success_within_3_months_pct",
    }
    assert required <= payload["metrics"].keys()
