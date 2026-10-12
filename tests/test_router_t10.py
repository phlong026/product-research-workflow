from __future__ import annotations

import pytest

from research_workflow.competition import build_competition_metrics
from research_workflow.data.models import BSRRecord
from research_workflow.metrics import build_market_metrics
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products
from research_workflow.router import ResearchRouter, RouterConfig, confirmed_modules
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


def fixture_metrics():
    rows = [
        BSRRecord("N", "2025-01", 20, node_rank=1, brand="N"),
        BSRRecord("B", "2025-01", 80, node_rank=2, brand="B"),
        BSRRecord("N", "2026-01", 25, node_rank=1, brand="N"),
        BSRRecord("B", "2026-01", 55, node_rank=2, brand="B"),
    ]
    market = build_market_metrics(
        rows,
        {"N": "nugget", "B": "bullet"},
        decomposition_pairs=[("2025-01", "2026-01")],
    )
    competition = build_competition_metrics(
        rows,
        rank_cutoffs=(1, 2),
        head_cutoff=1,
        broad_cutoff=2,
    )
    return market, competition


def test_router_suggests_research_branches_not_product_decisions():
    market, competition = fixture_metrics()
    plan = ResearchRouter().suggest(market, competition)
    ids = {x.route_id for x in plan.suggestions}
    assert "market_decline_deep_dive" in ids
    assert "segment_migration:nugget" in ids
    rendered = str(plan.to_dict()).lower()
    assert "建议进入" not in rendered
    assert "不建议进入" not in rendered
    assert plan.human_confirmed is False


def test_small_market_fast_growth_requires_explicit_project_thresholds():
    rows = [
        BSRRecord("A", "2025-01", 100),
        BSRRecord("A", "2026-01", 200),
    ]
    market = build_market_metrics(rows, {"A": "x"})
    assert not any(
        x.route_id == "small_market_fast_growth"
        for x in ResearchRouter().suggest(market).suggestions
    )
    plan = ResearchRouter(RouterConfig(small_market_sales_max=300, fast_growth_pct=50)).suggest(
        market
    )
    assert any(x.route_id == "small_market_fast_growth" for x in plan.suggestions)


def test_route_must_be_human_confirmed_before_modules_are_scheduled():
    market, competition = fixture_metrics()
    router = ResearchRouter()
    plan = router.suggest(market, competition)
    with pytest.raises(ValueError, match="human-confirmed"):
        confirmed_modules(plan)

    confirmed = router.confirm(plan, ["segment_migration:nugget"])
    modules = confirmed_modules(confirmed)
    assert modules == ["segment_deep_dive", "new_products", "external_channels"]


def test_unknown_route_cannot_be_injected_by_human_response():
    market, competition = fixture_metrics()
    router = ResearchRouter()
    plan = router.suggest(market, competition)
    with pytest.raises(ValueError, match="Unknown research route"):
        router.confirm(plan, ["final_yes"])


def test_router_uses_t01_human_checkpoint():
    market, competition = fixture_metrics()
    store = SQLiteStore()
    workflow = WorkflowService(store)
    project = workflow.create_project("ice maker", "US")
    router = ResearchRouter()
    plan = router.suggest(market, competition)
    cp = router.request_human_checkpoint(workflow, project.id, plan)
    assert cp.step_key == "research_route_review"
    assert workflow.snapshot(project.id)["project"]["status"] == "paused"


def test_router_has_manual_research_fallback_when_no_standard_branch_matches():
    rows = [BSRRecord("A", "2026-01", 100, brand="A", node_rank=1)]
    market = build_market_metrics(rows, {"A": "x"})
    plan = ResearchRouter().suggest(market)
    assert [x.route_id for x in plan.suggestions] == ["manual_research"]
    confirmed = ResearchRouter().confirm(plan, ["manual_research"])
    assert confirmed_modules(confirmed) == ["manual_research"]
