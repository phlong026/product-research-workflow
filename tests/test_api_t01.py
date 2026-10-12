from fastapi.testclient import TestClient

from research_workflow.api import create_app
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


def test_project_and_checkpoint_api(tmp_path):
    service = WorkflowService(SQLiteStore(tmp_path / "api.db"))
    client = TestClient(create_app(service, api_token="test-only"), client=("127.0.0.1", 50000),
                        headers={"Authorization": "Bearer test-only"})
    created = client.post("/projects", json={"product": "Ice Maker", "marketplace": "US"})
    assert created.status_code == 200
    project_id = created.json()["project"]["id"]
    cp = client.post(
        f"/projects/{project_id}/checkpoints",
        json={"step_key": "data_review", "prompt": "确认数据", "payload": {"rows": 100}},
    )
    assert cp.status_code == 200
    checkpoint_id = cp.json()["checkpoint_id"]
    resolved = client.post(
        f"/checkpoints/{checkpoint_id}/resolve",
        json={"approved": True, "response": {"reviewer": "human"}},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"
