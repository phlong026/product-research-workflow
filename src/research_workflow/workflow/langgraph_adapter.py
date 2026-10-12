from __future__ import annotations

from typing import Any

from research_workflow.workflow.state import ResearchState


def build_human_review_graph(checkpointer: Any | None = None):
    """Build the minimal LangGraph HITL graph used by T01.

    Import is lazy so offline tests do not require LangGraph. Domain state and
    business persistence remain outside this adapter.
    """
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ImportError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("Install workflow extras: pip install -e '.[workflow]'") from exc

    def human_review(state: ResearchState) -> dict:
        decision = interrupt(
            {
                "type": "human_review",
                "project_id": state["project_id"],
                "step": state.get("current_step", "human_review"),
                "message": "请检查当前研究阶段结果后确认是否继续。",
            }
        )
        return {"human_response": decision}

    graph = StateGraph(ResearchState)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "human_review")
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
