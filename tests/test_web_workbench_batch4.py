"""多人 Web 工作台：身份、项目权限、上传、后台任务、人工检查点、下载。"""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from research_workflow.web import WorkbenchStore, create_app

CSV = "ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地\nN1,100,NB,Nugget Maker,1,CN\nB1,50,BB,Bullet Maker,2,US\n"


@pytest.fixture()
def env(tmp_path):
    app = create_app(workbench_db=str(tmp_path / "wb.db"), engine_db=str(tmp_path / "engine.db"),
                     root=str(tmp_path / "projects"), start_worker=False, max_upload_mb=1)
    client = TestClient(app)
    store: WorkbenchStore = app.state.store
    admin, admin_t = store.create_user("admin1", "admin")
    alice, alice_t = store.create_user("alice", "member")
    bob, bob_t = store.create_user("bob", "member")

    class Env:
        pass

    e = Env()
    e.app, e.client, e.store, e.tmp = app, client, store, tmp_path
    e.h = lambda t: {"Authorization": f"Bearer {t}"}
    e.admin, e.alice, e.bob = admin_t, alice_t, bob_t

    def run_jobs():
        return app.state.worker.run_pending()

    e.run_jobs = run_jobs
    return e


def _create_project(e, token):
    r = e.client.post("/api/projects", json={"product": "Ice Maker", "marketplace": "US"}, headers=e.h(token))
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


def _upload(e, token, pid, name="2026-01.csv", data=CSV):
    r = e.client.put(f"/api/projects/{pid}/uploads", params={"filename": name}, content=data.encode(), headers=e.h(token))
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _job(e, token, pid, command, params=None):
    r = e.client.post(f"/api/projects/{pid}/jobs", json={"command": command, "params": params or {}}, headers=e.h(token))
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    e.run_jobs()
    job = e.client.get(f"/api/jobs/{jid}", headers=e.h(token)).json()
    return job


def test_auth_required_and_disabled_users_blocked(env):
    assert env.client.get("/api/me").status_code == 401
    assert env.client.get("/api/me", headers=env.h("nope")).status_code == 401
    assert env.client.get("/api/me", headers=env.h(env.alice)).json()["name"] == "alice"
    env.store.set_disabled("alice", True)
    assert env.client.get("/api/me", headers=env.h(env.alice)).status_code == 401
    # 令牌只存哈希
    assert env.alice not in open(env.tmp / "wb.db", "rb").read().decode("latin1")


def test_only_admin_manages_users(env):
    assert env.client.post("/api/users", json={"name": "x"}, headers=env.h(env.alice)).status_code == 403
    r = env.client.post("/api/users", json={"name": "carol", "role": "member"}, headers=env.h(env.admin))
    assert r.status_code == 200 and r.json()["token"]
    assert env.client.post("/api/users", json={"name": "carol"}, headers=env.h(env.admin)).status_code == 409


def test_project_isolation_and_roles(env):
    pid = _create_project(env, env.alice)
    assert env.client.get(f"/api/projects/{pid}/status", headers=env.h(env.bob)).status_code == 404
    assert pid in [p["project_id"] for p in env.client.get("/api/projects", headers=env.h(env.alice)).json()]
    assert env.client.get("/api/projects", headers=env.h(env.bob)).json() == []
    assert pid in [p["project_id"] for p in env.client.get("/api/projects", headers=env.h(env.admin)).json()]

    env.client.put(f"/api/projects/{pid}/members", json={"user": "bob", "role": "viewer"}, headers=env.h(env.alice))
    assert env.client.get(f"/api/projects/{pid}/status", headers=env.h(env.bob)).status_code == 200
    r = env.client.post(f"/api/projects/{pid}/jobs", json={"command": "dossier"}, headers=env.h(env.bob))
    assert r.status_code == 403
    assert env.client.put(f"/api/projects/{pid}/uploads", params={"filename": "a.csv"}, content=b"x", headers=env.h(env.bob)).status_code == 403
    assert env.client.put(f"/api/projects/{pid}/members", json={"user": "bob", "role": "editor"}, headers=env.h(env.bob)).status_code == 403
    env.client.put(f"/api/projects/{pid}/members", json={"user": "bob", "role": "editor"}, headers=env.h(env.alice))
    _upload(env, env.bob, pid)
    assert env.client.delete(f"/api/projects/{pid}/members/alice", headers=env.h(env.alice)).status_code == 409  # 唯一负责人


def test_upload_limits_and_safe_names(env):
    pid = _create_project(env, env.alice)
    big = env.client.put(f"/api/projects/{pid}/uploads", params={"filename": "big.csv"}, content=b"x" * (1024 * 1024 + 1), headers=env.h(env.alice))
    assert big.status_code == 413
    assert env.client.put(f"/api/projects/{pid}/uploads", params={"filename": "evil.exe"}, content=b"x", headers=env.h(env.alice)).status_code == 400
    assert env.client.put(f"/api/projects/{pid}/uploads", params={"filename": "e.csv"}, content=b"", headers=env.h(env.alice)).status_code == 400
    r = env.client.put(f"/api/projects/{pid}/uploads", params={"filename": "../../etc/pass wd.csv"}, content=b"a,b\n1,2\n", headers=env.h(env.alice))
    assert r.status_code == 200 and "/" not in r.json()["filename"] and ".." not in r.json()["filename"]
    uploads = list((env.tmp / "projects" / pid / "uploads").rglob("*.csv"))
    assert len(uploads) == 1 and (env.tmp / "projects" / pid) in uploads[0].parents


def test_command_whitelist_and_parameter_validation(env):
    pid = _create_project(env, env.alice)
    post = lambda body: env.client.post(f"/api/projects/{pid}/jobs", json=body, headers=env.h(env.alice))
    assert post({"command": "rm -rf /"}).status_code == 400
    assert post({"command": "classify", "params": {"label": ["a=b"], "evil": 1}}).status_code == 400
    assert post({"command": "ingest", "params": {"file": ["/etc/passwd"]}}).status_code == 400   # 只接受上传 ID
    assert post({"command": "ingest", "params": {"month": "2026-1", "file": []}}).status_code == 400
    assert post({"command": "classify", "params": {"label": ["a=b"], "provider": "gpt"}}).status_code == 400
    assert post({"command": "ingest", "params": {"live": True, "months": "2026-01"}}).status_code == 400  # live 未开启
    other = _create_project(env, env.bob)
    foreign = _upload(env, env.bob, other)
    assert post({"command": "ingest", "params": {"file": [foreign]}}).status_code == 400  # 他人项目的文件


def test_full_flow_with_checkpoints_audit_and_downloads(env):
    pid = _create_project(env, env.alice)
    up = _upload(env, env.alice, pid)
    job = _job(env, env.alice, pid, "ingest", {"file": [up]})
    assert job["status"] == "succeeded", job
    cp = job["result"]["checkpoint_id"]
    status = env.client.get(f"/api/projects/{pid}/status", headers=env.h(env.alice)).json()
    assert status["pending_checkpoint"]["checkpoint_id"] == cp

    r = env.client.post(f"/api/projects/{pid}/resolve", json={"checkpoint": cp, "approve": True}, headers=env.h(env.alice))
    env.run_jobs()
    assert env.client.get(f"/api/jobs/{r.json()['job_id']}", headers=env.h(env.alice)).json()["status"] == "succeeded"

    job = _job(env, env.alice, pid, "classify", {"label": ["nugget=咀嚼冰", "bullet=子弹冰"], "rule": ["nugget=nugget"]})
    assert job["status"] == "succeeded", job
    r = env.client.post(f"/api/projects/{pid}/resolve", json={"checkpoint": job["result"]["checkpoint_id"], "approve": True, "edits": ["B1=bullet"]}, headers=env.h(env.alice))
    env.run_jobs()
    job = _job(env, env.alice, pid, "analyze")
    assert job["status"] == "succeeded", job
    env.client.post(f"/api/projects/{pid}/resolve", json={"checkpoint": job["result"]["checkpoint_id"], "approve": True, "select": ""}, headers=env.h(env.alice))
    env.run_jobs()
    job = _job(env, env.alice, pid, "dossier")
    assert job["status"] == "succeeded", job

    ex = env.client.get(f"/api/projects/{pid}/exports", headers=env.h(env.alice)).json()
    assert len(ex["dossier_job_ids"]) == 1
    z = env.client.get(f"/api/projects/{pid}/dossiers/{ex['dossier_job_ids'][0]}.zip", headers=env.h(env.alice))
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "workbook.xlsx" in names and "report.md" in names
    assert env.client.get(f"/api/projects/{pid}/dossiers/{ex['dossier_job_ids'][0]}.zip", headers=env.h(env.bob)).status_code == 404

    audit = env.client.get(f"/api/projects/{pid}/audit", headers=env.h(env.alice)).json()
    resolutions = [a for a in audit if a["action"] == "checkpoint_resolution_submitted"]
    assert len(resolutions) == 3 and {a["user"] for a in resolutions} == {"alice"}
    assert env.client.get(f"/api/projects/{pid}/audit", headers=env.h(env.bob)).status_code == 404

    # 表导出走队列，下载有路径保护
    job = _job(env, env.alice, pid, "table-export", {"table": 1, "format": "csv"})
    assert job["status"] == "succeeded"
    name = env.client.get(f"/api/projects/{pid}/exports", headers=env.h(env.alice)).json()["exports"][0]
    assert env.client.get(f"/api/projects/{pid}/exports/{name}", headers=env.h(env.alice)).status_code == 200
    assert env.client.get(f"/api/projects/{pid}/exports/..%2F..%2Fresearch.db", headers=env.h(env.alice)).status_code in (400, 404)


def test_failed_job_records_error_and_engine_guardrails_still_apply(env):
    pid = _create_project(env, env.alice)
    # 未经数据检查点确认就分类：引擎拒绝，工作台如实记录失败
    up = _upload(env, env.alice, pid)
    assert _job(env, env.alice, pid, "ingest", {"file": [up]})["status"] == "succeeded"
    job = _job(env, env.alice, pid, "classify", {"label": ["a=b"]})
    assert job["status"] == "failed" and "data_review" in job["error"]


def test_evidence_added_by_is_forced_to_current_user_and_dash_values_are_safe(env):
    pid = _create_project(env, env.alice)
    job = _job(env, env.alice, pid, "evidence-add", {
        "slot": "google_trends", "claim": "--project=evil 热度上升", "source_name": "GT", "source_url": "https://trends.google.com"})
    assert job["status"] == "succeeded", job
    assert job["result"]["evidence"]["added_by"] == "alice"
    assert job["result"]["evidence"]["claim"] == "--project=evil 热度上升"
    ledger = json.loads((env.tmp / "projects" / pid / "evidence_ledger.json").read_text(encoding="utf-8"))
    assert len(ledger["evidence"]) == 1
    assert not (env.tmp / "projects" / "evil").exists()


def test_interrupted_jobs_are_not_silently_rerun(env):
    pid = _create_project(env, env.alice)
    jid = env.store.enqueue(project_id=pid, command="projects", args={"argv": [], "timeout": 5}, user="alice")
    env.store.claim_next()  # 模拟执行中进程崩溃
    assert env.store.interrupt_running() == 1
    job = env.store.get_job(jid)
    assert job["status"] == "failed" and "中断" in job["error"]


def test_index_page_and_security_headers(env):
    r = env.client.get("/")
    assert r.status_code == 200 and "选品研究工作台" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert env.client.get("/static/app.js").status_code == 200
