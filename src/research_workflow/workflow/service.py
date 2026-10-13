from __future__ import annotations

from dataclasses import asdict
from functools import wraps
from uuid import uuid4

from research_workflow.domain import (
    CheckpointStatus,
    HumanCheckpoint,
    ProjectStatus,
    ResearchProject,
    RunStatus,
    StepStatus,
    WorkflowRun,
    WorkflowStep,
)
from research_workflow.domain.models import utc_now_iso
from research_workflow.store import SQLiteStore
from research_workflow.workflow.state import initial_state


def _atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return wrapped


class WorkflowService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store


    def create_run(self, project_id: str, graph_version: str = "v1") -> WorkflowRun:
        project = self._require_project(project_id)
        run_id = str(uuid4())
        state = initial_state(project.id, project.product, project.marketplace)
        state["current_step"] = "project_created"
        run = WorkflowRun(
            id=run_id,
            project_id=project_id,
            graph_version=graph_version or "v1",
            thread_id=run_id,
            state_payload=dict(state),
        )
        self.store.create_run(run)
        return run

    def get_run_snapshot(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run: {run_id}")
        return {
            "run": asdict(run) | {"status": run.status.value},
            "steps": self.store.list_run_steps(run_id),
        }

    @_atomic
    def start_run(self, run_id: str) -> WorkflowRun:
        run = self._require_run(run_id)
        self._guard_pending_review(run.project_id)
        if run.status in {RunStatus.RUNNING, RunStatus.COMPLETED}:
            return run
        now = utc_now_iso()
        updated = self.store.update_run(
            run_id,
            status=RunStatus.RUNNING,
            started_at=run.started_at or now,
            updated_at=now,
        )
        self.store.update_project_status(run.project_id, ProjectStatus.RUNNING, now)
        return updated

    def mark_run_step(
        self,
        run_id: str,
        step_key: str,
        status: StepStatus,
        *,
        output_ref: str | None = None,
        error: str | None = None,
    ) -> WorkflowRun:
        run = self._require_run(run_id)
        now = utc_now_iso()
        self.store.upsert_run_step(
            run_id,
            step_key,
            status,
            output_ref=output_ref,
            error=error,
            updated_at=now,
        )
        state = dict(run.state_payload)
        state["current_step"] = step_key
        completed = list(state.get("completed_steps", []))
        if status is StepStatus.COMPLETED and step_key not in completed:
            completed.append(step_key)
        if status is not StepStatus.COMPLETED and step_key in completed:
            completed.remove(step_key)
        state["completed_steps"] = completed
        if output_ref:
            refs = dict(state.get("data_refs", {}))
            refs[step_key] = output_ref
            state["data_refs"] = refs

        run_status = run.status
        ended_at = run.ended_at
        if status is StepStatus.FAILED:
            run_status = RunStatus.FAILED
            ended_at = now
            self.store.update_project_status(run.project_id, ProjectStatus.FAILED, now)
        elif status is StepStatus.WAITING_HUMAN:
            run_status = RunStatus.PAUSED
            self.store.update_project_status(run.project_id, ProjectStatus.PAUSED, now)
        elif status is StepStatus.COMPLETED and step_key == "dossier":
            run_status = RunStatus.COMPLETED
            ended_at = now
            self.store.update_project_status(run.project_id, ProjectStatus.COMPLETED, now)

        return self.store.update_run(
            run_id,
            status=run_status,
            current_step=step_key,
            state_payload=state,
            ended_at=ended_at,
            updated_at=now,
        )

    def pause_run(self, run_id: str, step_key: str) -> WorkflowRun:
        return self.mark_run_step(run_id, step_key, StepStatus.WAITING_HUMAN)

    @_atomic
    def resume_run(self, run_id: str, *, resume_key: str | None = None) -> WorkflowRun:
        run = self._require_run(run_id)
        self._guard_pending_review(run.project_id)
        if run.status is RunStatus.FAILED:
            raise ValueError("Failed run must be retried as a new run")
        if resume_key:
            if not self.store.record_resume_key(run_id, resume_key, utc_now_iso()):
                return self._require_run(run_id)
        if run.status is RunStatus.COMPLETED:
            return run
        if run.status is RunStatus.FAILED:
            raise ValueError("Failed run must be retried as a new run")
        now = utc_now_iso()
        updated = self.store.update_run(
            run_id,
            status=RunStatus.RUNNING,
            updated_at=now,
        )
        self.store.update_project_status(run.project_id, ProjectStatus.RUNNING, now)
        return updated

    def retry_run(self, run_id: str) -> WorkflowRun:
        original = self._require_run(run_id)
        if original.status not in {RunStatus.FAILED, RunStatus.PAUSED}:
            raise ValueError("Only failed or paused runs can be retried")
        new_id = str(uuid4())
        state = dict(original.state_payload)
        state["pending_checkpoint_id"] = None
        state["human_response"] = None
        retry = WorkflowRun(
            id=new_id,
            project_id=original.project_id,
            graph_version=original.graph_version,
            thread_id=new_id,
            retry_of=original.id,
            current_step=original.current_step,
            state_payload=state,
        )
        self.store.create_run(retry)
        return retry

    def create_project(
        self,
        product: str,
        marketplace: str,
        *,
        primary_keyword: str | None = None,
        main_node: str | None = None,
    ) -> ResearchProject:
        product = product.strip()
        marketplace = marketplace.strip().upper()
        if not product:
            raise ValueError("product is required")
        if not marketplace:
            raise ValueError("marketplace is required")
        project = ResearchProject(
            id=str(uuid4()),
            product=product,
            marketplace=marketplace,
            primary_keyword=primary_keyword,
            main_node=main_node,
        )
        self.store.create_project(project)
        self.store.put_state(project.id, initial_state(project.id, product, marketplace))
        self.store.upsert_step(
            WorkflowStep(project_id=project.id, step_key="project_created", status=StepStatus.COMPLETED)
        )
        return project

    @_atomic
    def start(self, project_id: str) -> None:
        project = self._require_project(project_id)
        self._guard_pending_review(project_id)
        if project.status not in {ProjectStatus.DRAFT, ProjectStatus.PAUSED}:
            return
        now = utc_now_iso()
        self.store.update_project_status(project_id, ProjectStatus.RUNNING, now)
        state = self.store.get_state(project_id)
        state["current_step"] = "research_started"
        self.store.put_state(project_id, state)

    @_atomic
    def set_step(
        self,
        project_id: str,
        step_key: str,
        status: StepStatus,
        *,
        output_ref: str | None = None,
        error: str | None = None,
    ) -> WorkflowStep:
        self._require_project(project_id)
        step = WorkflowStep(
            project_id=project_id,
            step_key=step_key,
            status=status,
            output_ref=output_ref,
            error=error,
        )
        self.store.upsert_step(step)
        state = self.store.get_state(project_id)
        state["current_step"] = step_key
        completed = list(state.get("completed_steps", []))
        if status is StepStatus.COMPLETED and step_key not in completed:
            completed.append(step_key)
        if status is not StepStatus.COMPLETED and step_key in completed:
            completed.remove(step_key)
        state["completed_steps"] = completed
        self.store.put_state(project_id, state)
        return step

    @_atomic
    def request_human_checkpoint(
        self,
        project_id: str,
        step_key: str,
        prompt: str,
        payload: dict,
    ) -> HumanCheckpoint:
        self._require_project(project_id)
        state = self.store.get_state(project_id)
        previous = state.get("pending_checkpoint_id")
        if previous:
            self.store.resolve_checkpoint(previous, CheckpointStatus.RESOLVED,
                                          {"superseded": True}, utc_now_iso())
        approvals = dict(state.get("approved_checkpoints", {}))
        approvals.pop(step_key, None)
        state["approved_checkpoints"] = approvals
        self.store.put_state(project_id, state)
        cp = HumanCheckpoint(
            id=str(uuid4()),
            project_id=project_id,
            step_key=step_key,
            prompt=prompt,
            payload=payload,
        )
        self.store.create_checkpoint(cp)
        self.set_step(project_id, step_key, StepStatus.WAITING_HUMAN)
        now = utc_now_iso()
        self.store.update_project_status(project_id, ProjectStatus.PAUSED, now)
        state = self.store.get_state(project_id)
        state["pending_checkpoint_id"] = cp.id
        self.store.put_state(project_id, state)
        return cp

    @_atomic
    def resolve_human_checkpoint(
        self,
        checkpoint_id: str,
        *,
        approved: bool,
        response: dict | None = None,
    ) -> HumanCheckpoint:
        if type(approved) is not bool:
            raise ValueError("approved must be an explicit boolean")
        cp = self.store.get_checkpoint(checkpoint_id)
        if cp is None:
            raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
        if self.store.get_state(cp.project_id).get("pending_checkpoint_id") != cp.id:
            raise ValueError("Checkpoint is no longer current; inspect status")
        if cp.status is not CheckpointStatus.PENDING:
            raise ValueError("Checkpoint is already resolved")
        status = CheckpointStatus.APPROVED if approved else CheckpointStatus.REJECTED
        resolved = self.store.resolve_checkpoint(
            checkpoint_id,
            status,
            response or {},
            utc_now_iso(),
        )
        state = self.store.get_state(cp.project_id)
        state["pending_checkpoint_id"] = None
        state["human_response"] = response or {}
        approvals = dict(state.get("approved_checkpoints", {}))
        if approved:
            approvals[cp.step_key] = cp.id
        else:
            approvals.pop(cp.step_key, None)
        state["approved_checkpoints"] = approvals
        self.store.put_state(cp.project_id, state)
        if approved:
            self.set_step(cp.project_id, cp.step_key, StepStatus.COMPLETED)
            self.store.update_project_status(cp.project_id, ProjectStatus.RUNNING, utc_now_iso())
        else:
            self.set_step(cp.project_id, cp.step_key, StepStatus.PENDING)
            self.store.update_project_status(cp.project_id, ProjectStatus.PAUSED, utc_now_iso())
        return resolved

    def _guard_pending_review(self, project_id: str) -> None:
        if self.store.get_state(project_id).get("pending_checkpoint_id"):
            raise ValueError("Resolve the current human checkpoint before changing execution status")

    @_atomic
    def invalidate_steps(self, project_id: str, step_keys: list[str]) -> None:
        """Retain checkpoint audit history while making derived results unusable."""
        self._require_project(project_id)
        state = self.store.get_state(project_id)
        pending = state.get("pending_checkpoint_id")
        if pending:
            cp = self.store.get_checkpoint(pending)
            if cp and cp.step_key in step_keys:
                self.store.resolve_checkpoint(cp.id, CheckpointStatus.RESOLVED,
                                              {"superseded": True}, utc_now_iso())
                state["pending_checkpoint_id"] = None
        state["completed_steps"] = [s for s in state.get("completed_steps", []) if s not in step_keys]
        state["approved_checkpoints"] = {k: v for k, v in state.get("approved_checkpoints", {}).items()
                                         if k not in step_keys}
        state["human_response"] = None
        self.store.put_state(project_id, state)
        for key in step_keys:
            self.set_step(project_id, key, StepStatus.PENDING)
        self.store.update_project_status(project_id, ProjectStatus.PAUSED, utc_now_iso())

    def snapshot(self, project_id: str) -> dict:
        project = self._require_project(project_id)
        return {
            "project": asdict(project) | {"status": project.status.value},
            "state": self.store.get_state(project_id),
            "steps": [asdict(s) | {"status": s.status.value} for s in self.store.list_steps(project_id)],
        }

    def _require_run(self, run_id: str) -> WorkflowRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run: {run_id}")
        return run

    def _require_project(self, project_id: str) -> ResearchProject:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"Unknown project: {project_id}")
        return project
