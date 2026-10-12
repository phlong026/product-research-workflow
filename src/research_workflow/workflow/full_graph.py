from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from research_workflow.workflow.state import ResearchState

NodeHandler = Callable[[ResearchState], dict[str, Any]]


@dataclass(slots=True)
class ResearchGraphHandlers:
    """Runtime handlers for the core Research Workflow.

    Handlers exchange only small state updates / artifact refs. Heavy tables stay
    in the project artifact store or database, not inside LangGraph state.
    """

    collect_bsr: NodeHandler
    enrich_asins: NodeHandler
    classify: NodeHandler
    apply_classification_review: NodeHandler
    market_metrics: NodeHandler
    market_analysis: NodeHandler
    competition_metrics: NodeHandler
    new_product_analysis: NodeHandler
    research_route: NodeHandler
    apply_route_review: NodeHandler
    dossier: NodeHandler


CORE_WORKFLOW_STAGES: tuple[str, ...] = (
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

HUMAN_REVIEW_STAGES = frozenset({"data_review", "classification_review", "route_review"})


def build_core_research_graph(
    handlers: ResearchGraphHandlers,
    checkpointer: Any | None = None,
):
    """Build the core T01–T11 LangGraph.

    Optional downstream research modules (TikTok, keyword seasonality, ads, etc.)
    are intentionally outside this core graph and can be scheduled from the
    confirmed research route later.
    """
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ImportError as exc:  # pragma: no cover - runtime dependency boundary
        raise RuntimeError("Install workflow extras: pip install -e '.[workflow]'") from exc

    def review_node(stage: str, message: str):
        def review(state: ResearchState) -> dict[str, Any]:
            response = interrupt(
                {
                    "type": stage,
                    "project_id": state["project_id"],
                    "step": stage,
                    "message": message,
                    "data_refs": state.get("data_refs", {}),
                    "metrics_refs": state.get("metrics_refs", {}),
                    "analysis_refs": state.get("analysis_refs", {}),
                    "route_suggestions": state.get("route_suggestions", []),
                }
            )
            return {"human_response": response, "current_step": stage}

        return review

    graph = StateGraph(ResearchState)
    graph.add_node("collect_bsr", handlers.collect_bsr)
    graph.add_node(
        "data_review",
        review_node("data_review", "请检查清洗后的月度数据，确认后继续。"),
    )
    graph.add_node("enrich_asins", handlers.enrich_asins)
    graph.add_node("classify", handlers.classify)
    graph.add_node(
        "classification_review",
        review_node("classification_review", "请检查产品分类，可修改后确认继续。"),
    )
    graph.add_node("apply_classification_review", handlers.apply_classification_review)
    graph.add_node("market_metrics", handlers.market_metrics)
    graph.add_node("market_analysis", handlers.market_analysis)
    graph.add_node("competition_metrics", handlers.competition_metrics)
    graph.add_node("new_product_analysis", handlers.new_product_analysis)
    graph.add_node("research_route", handlers.research_route)
    graph.add_node(
        "route_review",
        review_node("route_review", "请确认下一步研究分支；可不选择任何支线。"),
    )
    graph.add_node("apply_route_review", handlers.apply_route_review)
    graph.add_node("dossier", handlers.dossier)

    graph.add_edge(START, "collect_bsr")
    graph.add_edge("collect_bsr", "data_review")
    graph.add_conditional_edges(
        "data_review",
        _approval_edge,
        {"continue": "enrich_asins", "stop": END},
    )
    graph.add_edge("enrich_asins", "classify")
    graph.add_edge("classify", "classification_review")
    graph.add_conditional_edges(
        "classification_review",
        _approval_edge,
        {"continue": "apply_classification_review", "stop": END},
    )
    graph.add_edge("apply_classification_review", "market_metrics")
    graph.add_edge("market_metrics", "market_analysis")
    graph.add_edge("market_analysis", "competition_metrics")
    graph.add_edge("competition_metrics", "new_product_analysis")
    graph.add_edge("new_product_analysis", "research_route")
    graph.add_edge("research_route", "route_review")
    graph.add_conditional_edges(
        "route_review",
        _approval_edge,
        {"continue": "apply_route_review", "stop": END},
    )
    graph.add_edge("apply_route_review", "dossier")
    graph.add_edge("dossier", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _approval_edge(state: ResearchState) -> str:
    return "continue" if _is_approved(state.get("human_response")) else "stop"


def _is_approved(response: Any) -> bool:
    if response is True:
        return True
    if response is False or response is None:
        return False
    if isinstance(response, dict):
        return bool(response.get("approved"))
    return False
