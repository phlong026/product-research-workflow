from __future__ import annotations

import asyncio
import json

from research_workflow.workflow.full_graph import (
    CORE_WORKFLOW_STAGES,
    HUMAN_REVIEW_STAGES,
    _is_approved,
)
from scripts.run_offline_demo import run


def test_core_workflow_stage_order_covers_t01_to_t11_contracts():
    assert CORE_WORKFLOW_STAGES == (
        "collect_bsr",
        "data_review",
        "enrich_asins",
        "classify",
        "classification_review",
        "apply_classification_review",
        "market_metrics",
        "market_analysis",
        "competition_metrics",
        "new_product_analysis",
        "research_route",
        "route_review",
        "apply_route_review",
        "dossier",
    )
    assert HUMAN_REVIEW_STAGES == {
        "data_review",
        "classification_review",
        "route_review",
    }


def test_human_review_requires_explicit_approval():
    assert _is_approved({"approved": True}) is True
    assert _is_approved({"approved": False}) is False
    assert _is_approved({"edits": {"A": "nugget"}}) is False
    assert _is_approved(None) is False


def test_offline_pipeline_runs_end_to_end_and_generates_dossier(tmp_path):
    root = asyncio.run(run(tmp_path / "dossier"))
    payload = json.loads((root / "data.json").read_text(encoding="utf-8"))
    assert payload["project"]["decision_owner"] == "human"
    assert payload["market_metrics"]["market"][-1]["month"] == "2026-01"
    assert payload["market_analysis"]["findings"]
    assert payload["competition_metrics"]["concentration"]
    assert payload["new_product_analysis"]["summary"]["successful_products"] == 1
    assert payload["research_route_plan"]["human_confirmed"] is True
    assert (root / "manifest.json").exists()
