"""Web 研究工作台后端(T4-1 作业执行 / T4-3 多人隔离):账号、项目隔离、CSRF、作业、重试、下载。"""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from research_workflow.web import create_web_app
from research_workflow.web import security
from research_workflow.web.store import WebStore

CSV = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地
N1,20,NBrand,Nugget Ice Maker Countertop,1,CN
B1,80,BBrand,Bullet Ice Maker for Home,2,US
"""


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(security, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "projects"
    web_store = WebStore(tmp_path / "web.db")
    web_store.create_user("alice", "alice-pass-123", "researcher")
    web_store.create_user("bob", "bob-pass-12345", "researcher")
    web_store.create_user("root", "root-pass-12345", "admin")
    app = create_web_app(db_path=tmp_path / "research.db", root=root, web_db_path=tmp_path / "web.db",
                         secure_cookies=False, job_workers=2)
    return app, tmp_path


class Session:
    def __init__(self, app, username: str, password: str) -> None:
        self.client = TestClient(app)
        resp = self.client.post("/api/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        self.csrf = resp.json()["csrf_token"]

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, json=None, csrf=True, **kw):
        headers = {"X-CSRF-Token": self.csrf} if csrf else {}
        return self.client.post(url, json=json, headers=headers, **kw)

    def put(self, url, content, csrf=True, **kw):
        headers = {"X-CSRF-Token": self.csrf} if csrf else {}
        return self.client.put(url, content=content, headers=headers, **kw)

    def wait(self, job_id: str, timeout: float = 90.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    def run_job(self, pid: str, command: str, params: dict | None = None) -> dict:
        resp = self.post(f"/api/projects/{pid}/jobs", {"command": command, "params": params or {}})
        assert resp.status_code == 202, resp.text
        return self.wait(resp.json()["job_id"])


def new_project(session: Session) -> str:
    resp = session.post("/api/projects", {"product": "Ice Maker", "marketplace": "US"})
    assert resp.status_code == 201, resp.text
    return resp.json()["project_id"]


def test_auth_required_and_login_failures(env) -> None:
    app, _ = env
    anon = TestClient(app)
    assert anon.get("/api/projects").status_code == 401
    assert anon.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401
    session = Session(app, "alice", "alice-pass-123")
    assert session.get("/api/me").json()["username"] == "alice"
    assert "password" not in session.get("/api/me").text


def test_login_lockout_after_repeated_failures(env) -> None:
    app, _ = env
    client = TestClient(app)
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "bob", "password": "bad"})
    locked = client.post("/api/auth/login", json={"username": "bob", "password": "bob-pass-12345"})
    assert locked.status_code == 429


def test_mutations_require_csrf_token(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    assert alice.post("/api/projects", {"product": "x", "marketplace": "US"}, csrf=False).status_code == 403


def test_projects_are_isolated_between_users(env) -> None:
    app, _ = env
    alice, bob = Session(app, "alice", "alice-pass-123"), Session(app, "bob", "bob-pass-12345")
    pid = new_project(alice)
    assert [p["project_id"] for p in alice.get("/api/projects").json()["projects"]] == [pid]
    assert bob.get("/api/projects").json()["projects"] == []
    assert bob.get(f"/api/projects/{pid}").status_code == 404  # 不泄露存在性
    assert bob.post(f"/api/projects/{pid}/jobs", {"command": "analyze", "params": {}}).status_code == 404
    assert bob.get(f"/api/projects/{pid}/artifacts/classification.json").status_code == 404
    admin = Session(app, "root", "root-pass-12345")
    assert admin.get(f"/api/projects/{pid}").status_code == 200


def test_sharing_roles_viewer_cannot_run_jobs(env) -> None:
    app, _ = env
    alice, bob = Session(app, "alice", "alice-pass-123"), Session(app, "bob", "bob-pass-12345")
    pid = new_project(alice)
    assert bob.post(f"/api/projects/{pid}/members", {"username": "bob", "role": "viewer"}).status_code == 404
    assert alice.post(f"/api/projects/{pid}/members", {"username": "bob", "role": "viewer"}).status_code == 200
    assert bob.get(f"/api/projects/{pid}").status_code == 200
    assert bob.post(f"/api/projects/{pid}/jobs", {"command": "analyze", "params": {}}).status_code == 403
    assert alice.post(f"/api/projects/{pid}/members", {"username": "bob", "role": "editor"}).status_code == 200
    # editor 不能再分享
    assert bob.post(f"/api/projects/{pid}/members", {"username": "root", "role": "viewer"}).status_code == 403


def test_full_research_flow_through_jobs(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)

    up = alice.put(f"/api/projects/{pid}/uploads?filename=2026-01.csv", CSV.encode("utf-8"))
    assert up.status_code == 201, up.text
    upload_id = up.json()["upload_id"]

    job = alice.run_job(pid, "ingest", {"uploads": [upload_id]})
    assert job["status"] == "succeeded", job
    state = alice.get(f"/api/projects/{pid}").json()
    cp = state["pending_checkpoint"]
    assert cp["step_key"] == "data_review"

    job = alice.run_job(pid, "resolve", {"checkpoint_id": cp["checkpoint_id"], "approve": True})
    assert job["status"] == "succeeded", job

    job = alice.run_job(pid, "classify", {"labels": {"nugget": "Nugget", "bullet": "Bullet"},
                                          "rules": {"nugget": "nugget", "bullet": "bullet"}})
    assert job["status"] == "succeeded", job
    cp = alice.get(f"/api/projects/{pid}").json()["pending_checkpoint"]
    assert cp["step_key"] == "classification_review"
    classification = alice.get(f"/api/projects/{pid}/artifacts/classification.json").json()
    assert set(classification["batch"]["rows"]) == {"N1", "B1"}

    job = alice.run_job(pid, "resolve", {"checkpoint_id": cp["checkpoint_id"], "approve": True,
                                         "edits": {"B1": "bullet"}})
    assert job["status"] == "succeeded", job
    assert alice.run_job(pid, "analyze", {})["status"] == "succeeded"
    cp = alice.get(f"/api/projects/{pid}").json()["pending_checkpoint"]
    job = alice.run_job(pid, "resolve", {"checkpoint_id": cp["checkpoint_id"], "approve": True, "select": []})
    assert job["status"] == "succeeded", job

    job = alice.run_job(pid, "dossier", {})
    assert job["status"] == "succeeded", job
    listing = alice.get(f"/api/projects/{pid}/dossiers").json()["dossiers"]
    assert len(listing) == 1
    archive = alice.get(f"/api/projects/{pid}/dossiers/{listing[0]['version']}/download.zip")
    assert archive.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert "report.md" in names and "manifest.json" in names
    assert alice.get(f"/api/projects/{pid}").json()["next"] == "done"
    jobs = alice.get(f"/api/projects/{pid}/jobs").json()["jobs"]
    assert [j["command"] for j in jobs][-1] == "dossier" or jobs[0]["command"] == "dossier"


def test_failed_job_reports_error_and_can_be_retried(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    job = alice.run_job(pid, "classify", {"labels": {"a": "A"}})  # 尚未导入数据 → 引擎报错
    assert job["status"] == "failed" and job["error"]
    retry = alice.post(f"/api/jobs/{job['job_id']}/retry")
    assert retry.status_code == 202
    again = alice.wait(retry.json()["job_id"])
    assert again["status"] == "failed" and again["attempts"] == 2


def test_one_active_job_per_project(env) -> None:
    app, tmp_path = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    store = WebStore(tmp_path / "web.db")
    store.insert_job(project_id=pid, command="ingest", params={}, created_by="x", status="running")
    resp = alice.post(f"/api/projects/{pid}/jobs", {"command": "analyze", "params": {}})
    assert resp.status_code == 409


def test_interrupted_jobs_are_marked_failed_on_restart(env, tmp_path) -> None:
    store = WebStore(tmp_path / "web.db")
    job_id = store.insert_job(project_id="p", command="ingest", params={}, created_by="x", status="running")
    app = create_web_app(db_path=tmp_path / "research.db", root=tmp_path / "projects",
                         web_db_path=tmp_path / "web.db", secure_cookies=False)
    job = store.get_job(job_id)
    assert job["status"] == "failed" and "中断" in job["error"]
    assert app is not None


def test_input_validation_and_path_safety(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    assert alice.post(f"/api/projects/{pid}/jobs", {"command": "rm -rf", "params": {}}).status_code == 422
    assert alice.post(f"/api/projects/{pid}/jobs",
                      {"command": "ingest", "params": {"uploads": ["nope"]}}).status_code == 422
    assert alice.get(f"/api/projects/{pid}/artifacts/..%2F..%2Fresearch.db").status_code == 404
    assert alice.get(f"/api/projects/{pid}/artifacts/secrets.txt").status_code == 404
    assert alice.put(f"/api/projects/{pid}/uploads?filename=evil.exe", b"x").status_code == 415
    ok = alice.put(f"/api/projects/{pid}/uploads?filename=../../x.csv", b"ASIN\nA\n")
    assert ok.status_code == 201 and "/" not in ok.json()["filename"]
    assert alice.get(f"/api/projects/{pid}/dossiers/..%2F..%2Fx/download.zip").status_code == 404


def test_admin_user_management(env) -> None:
    app, _ = env
    alice, admin = Session(app, "alice", "alice-pass-123"), Session(app, "root", "root-pass-12345")
    body = {"username": "carol", "password": "carol-pass-12345", "role": "researcher"}
    assert alice.post("/api/admin/users", body).status_code == 403
    assert admin.post("/api/admin/users", body).status_code == 201
    assert admin.post("/api/admin/users", {**body, "password": "short"}).status_code == 422
    Session(app, "carol", "carol-pass-12345")


def test_table_export_import_roundtrip_via_jobs(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    up = alice.put(f"/api/projects/{pid}/uploads?filename=2026-01.csv", CSV.encode("utf-8"))
    assert alice.run_job(pid, "ingest", {"uploads": [up.json()["upload_id"]]})["status"] == "succeeded"
    cp = alice.get(f"/api/projects/{pid}").json()["pending_checkpoint"]
    assert alice.run_job(pid, "resolve", {"checkpoint_id": cp["checkpoint_id"], "approve": True})["status"] == "succeeded"

    assert alice.run_job(pid, "export-table", {"table": 1})["status"] == "succeeded"
    exported = alice.get(f"/api/projects/{pid}/exports/table1.csv")
    assert exported.status_code == 200 and "monthly_sales" in exported.text
    assert alice.get(f"/api/projects/{pid}/exports/table9.csv").status_code == 404
    assert alice.get(f"/api/projects/{pid}/exports/..%2Frecords.json").status_code == 404

    edited = exported.text.replace(",80,", ",81,")
    assert edited != exported.text
    edited_up = alice.put(f"/api/projects/{pid}/uploads?filename=table1-edit.csv", edited.encode("utf-8"))
    job = alice.run_job(pid, "import-table", {"table": 1, "upload": edited_up.json()["upload_id"]})
    assert job["status"] == "succeeded", job
    state = alice.get(f"/api/projects/{pid}").json()
    assert state["pending_checkpoint"]["step_key"] == "data_review"  # 旧审批失效,需重新确认
    assert state["pending_checkpoint"]["payload"]["source_mode"] == "human_table1"
    # 其他用户读不到导出
    bob = Session(app, "bob", "bob-pass-12345")
    assert bob.get(f"/api/projects/{pid}/exports/table1.csv").status_code == 404


def test_member_listing_and_removal(env) -> None:
    app, _ = env
    alice, bob = Session(app, "alice", "alice-pass-123"), Session(app, "bob", "bob-pass-12345")
    pid = new_project(alice)
    alice.post(f"/api/projects/{pid}/members", {"username": "bob", "role": "viewer"})
    members = alice.get(f"/api/projects/{pid}/members").json()["members"]
    assert {(m["username"], m["role"]) for m in members} == {("alice", "owner"), ("bob", "viewer")}
    assert bob.get(f"/api/projects/{pid}/members").status_code == 200  # 成员可查看名单
    assert bob.client.delete(f"/api/projects/{pid}/members/alice", headers={"X-CSRF-Token": bob.csrf}).status_code == 403
    assert alice.client.delete(f"/api/projects/{pid}/members/alice", headers={"X-CSRF-Token": alice.csrf}).status_code == 409
    assert alice.client.delete(f"/api/projects/{pid}/members/bob", headers={"X-CSRF-Token": alice.csrf}).status_code == 200
    assert bob.get(f"/api/projects/{pid}").status_code == 404
