from __future__ import annotations

from typing import Any, TypedDict


class ResearchState(TypedDict, total=False):
    project_id: str
    product: str
    marketplace: str
    primary_keyword: str | None
    main_node: str | None
    current_step: str
    completed_steps: list[str]
    pending_checkpoint_id: str | None
    data_refs: dict[str, str]
    metrics_refs: dict[str, str]
    analysis_refs: dict[str, str]
    route_suggestions: list[dict[str, Any]]
    human_response: dict[str, Any] | None


def initial_state(project_id: str, product: str, marketplace: str) -> ResearchState:
    return ResearchState(
        project_id=project_id,
        product=product,
        marketplace=marketplace,
        current_step="project_created",
        completed_steps=[],
        pending_checkpoint_id=None,
        data_refs={},
        metrics_refs={},
        analysis_refs={},
        route_suggestions=[],
        human_response=None,
    )
