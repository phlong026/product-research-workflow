from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from research_workflow.api.app import create_app
from research_workflow.domain import RunStatus, StepStatus
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


def test_run_state_survives_store_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    store = SQLiteStore(db)
    service = WorkflowService(store)
    project = service.create_project("ice maker", "US")
    run = service.create_run(project.id, "research-v1")
    service.start_run(run.id)
    service.mark_run_step(run.id, "collect_bsr", StepStatus.COMPLETED, output_ref="dataset://m1")
    service.pause_run(run.id, "data_review")
    run_id = run.id
    store.close()

    reopened = SQLiteStore(db)
    restored = WorkflowService(reopened).get_run_snapshot(run_id)
    assert restored["run"]["status"] == "paused"
    assert restored["run"]["state_payload"]["data_refs"]["collect_bsr"] == "dataset://m1"
    assert any(step["step_key"] == "data_review" for step in restored["steps"])


def test_resume_key_is_idempotent_and_failure_preserves_previous_outputs():
    service = WorkflowService(SQLiteStore())
    project = service.create_project("ice maker", "US")
    run = service.create_run(project.id)
    service.start_run(run.id)
    service.mark_run_step(run.id, "collect_bsr", StepStatus.COMPLETED, output_ref="dataset://x")
    service.pause_run(run.id, "data_review")

    first = service.resume_run(run.id, resume_key="same-command")
    second = service.resume_run(run.id, resume_key="same-command")
    assert first.id == second.id
    assert second.status is RunStatus.RUNNING

    failed = service.mark_run_step(run.id, "enrich_asins", StepStatus.FAILED, error="timeout")
    assert failed.status is RunStatus.FAILED
    snap = service.get_run_snapshot(run.id)
    assert snap["run"]["state_payload"]["data_refs"]["collect_bsr"] == "dataset://x"
    failed_step = next(s for s in snap["steps"] if s["step_key"] == "enrich_asins")
    assert failed_step["error"] == "timeout"


def test_retry_creates_new_run_and_retains_existing_state_refs():
    service = WorkflowService(SQLiteStore())
    project = service.create_project("ice maker", "US")
    run = service.create_run(project.id)
    service.start_run(run.id)
    service.mark_run_step(run.id, "collect_bsr", StepStatus.COMPLETED, output_ref="dataset://x")
    service.mark_run_step(run.id, "enrich_asins", StepStatus.FAILED, error="timeout")

    retry = service.retry_run(run.id)
    assert retry.id != run.id
    assert retry.retry_of == run.id
    assert retry.state_payload["data_refs"]["collect_bsr"] == "dataset://x"
    assert retry.status is RunStatus.CREATED


def test_run_api_contracts():
    service = WorkflowService(SQLiteStore())
    client = TestClient(create_app(service))
    project = client.post("/projects", json={"product": "ice maker", "marketplace": "US"}).json()
    project_id = project["project"]["id"]

    response = client.post(f"/projects/{project_id}/runs", json={"graph_version": "v1"})
    assert response.status_code == 200
    run_id = response.json()["run"]["id"]
    assert response.json()["run"]["status"] == "running"

    assert client.get(f"/runs/{run_id}").status_code == 200
    resumed = client.post(f"/runs/{run_id}/resume", json={"resume_key": "r1"})
    assert resumed.status_code == 200
