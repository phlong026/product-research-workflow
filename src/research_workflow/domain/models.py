from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CheckpointStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"


@dataclass(slots=True)
class ResearchProject:
    id: str
    product: str
    marketplace: str
    status: ProjectStatus = ProjectStatus.DRAFT
    primary_keyword: str | None = None
    main_node: str | None = None
    research_start: str | None = None
    research_end: str | None = None
    final_decision_owner: str = "human"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class WorkflowRun:
    id: str
    project_id: str
    graph_version: str = "v1"
    status: RunStatus = RunStatus.CREATED
    thread_id: str = ""
    retry_of: str | None = None
    current_step: str = "project_created"
    state_payload: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class WorkflowStep:
    project_id: str
    step_key: str
    status: StepStatus = StepStatus.PENDING
    output_ref: str | None = None
    error: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class HumanCheckpoint:
    id: str
    project_id: str
    step_key: str
    prompt: str
    payload: dict[str, Any]
    status: CheckpointStatus = CheckpointStatus.PENDING
    response: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now_iso)
    resolved_at: str | None = None
