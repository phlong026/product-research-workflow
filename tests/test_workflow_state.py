from pathlib import Path

from research_workflow.domain import ProjectStatus, StepStatus
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


def test_project_state_and_checkpoint_survive_store_reopen(tmp_path: Path):
    db = tmp_path / "workflow.db"
    store = SQLiteStore(db)
    service = WorkflowService(store)
    project = service.create_project("Ice Maker", "us", primary_keyword="ice maker")
    assert project.final_decision_owner == "human"
    service.start(project.id)
    cp = service.request_human_checkpoint(
        project.id,
        "data_review",
        "确认清洗数据",
        {"dataset_ref": "artifact://cleaned/1"},
    )
    assert service.snapshot(project.id)["project"]["status"] == ProjectStatus.PAUSED.value
    store.close()

    store2 = SQLiteStore(db)
    service2 = WorkflowService(store2)
    snap = service2.snapshot(project.id)
    assert snap["state"]["pending_checkpoint_id"] == cp.id
    assert any(s["status"] == StepStatus.WAITING_HUMAN.value for s in snap["steps"])
    service2.resolve_human_checkpoint(cp.id, approved=True, response={"reviewer": "human"})
    snap2 = service2.snapshot(project.id)
    assert snap2["project"]["status"] == ProjectStatus.RUNNING.value
    assert snap2["state"]["pending_checkpoint_id"] is None
    assert snap2["state"]["human_response"] == {"reviewer": "human"}
    store2.close()


def test_rejected_checkpoint_pauses_for_correction(tmp_path: Path):
    service = WorkflowService(SQLiteStore(tmp_path / "w.db"))
    p = service.create_project("Ice Maker", "US")
    cp = service.request_human_checkpoint(p.id, "classification_review", "确认分类", {})
    service.resolve_human_checkpoint(cp.id, approved=False, response={"reason": "needs correction"})
    snap = service.snapshot(p.id)
    assert snap["project"]["status"] == "paused"
    assert [x for x in snap["steps"] if x["step_key"] == "classification_review"][0]["status"] == "pending"
