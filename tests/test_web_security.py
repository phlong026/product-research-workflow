"""Web 后端安全评审(HIGH/MEDIUM)的回归测试:限流绕过、信息泄露、上传 DoS、路径穿越。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from research_workflow.web import create_web_app, jobs, security
from research_workflow.web.store import WebStore

from test_web_api import CSV, Session, new_project  # noqa: F401  (复用夹具与辅助函数)
from test_web_api import env, fast_hash  # noqa: F401


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


@pytest.mark.parametrize("variant", ["alice", " alice", "alice\t", "　alice", " alice", "ALICE", "Alice "])
def test_username_variants_share_one_throttle_bucket(env, variant) -> None:
    app, _ = env
    client = TestClient(app)
    for _ in range(5):
        _login(client, variant, "wrong-password")
    # 无论用哪种写法,同一账号都已被锁定(包括正确口令)
    assert _login(client, "alice", "alice-pass-123").status_code == 429


def test_normalized_username_can_login(env) -> None:
    app, _ = env
    assert _login(TestClient(app), "　ALICE ", "alice-pass-123").status_code == 200


def test_attacker_ip_cannot_lock_victim_from_other_ip(env) -> None:
    app, _ = env
    attacker = TestClient(app, client=("203.0.113.9", 4000))
    for _ in range(5):
        _login(attacker, "bob", "bad")
    assert _login(attacker, "bob", "bob-pass-12345").status_code == 429
    victim = TestClient(app, client=("198.51.100.7", 4000))
    assert _login(victim, "bob", "bob-pass-12345").status_code == 200


def test_per_ip_cap_blocks_credential_stuffing_across_usernames(env) -> None:
    app, _ = env
    client = TestClient(app, client=("203.0.113.50", 1))
    for i in range(security.MAX_FAILED_PER_IP):
        _login(client, f"ghost{i}", "x")
    assert _login(client, "alice", "alice-pass-123").status_code == 429


def test_throttle_table_is_bounded() -> None:
    throttle = security.LoginThrottle(capacity=50)
    for i in range(500):
        throttle.record_failure(f"user{i}")
    assert len(throttle._failures) <= 50


def test_password_checks_are_concurrency_limited(env) -> None:
    app, _ = env
    held = [security.password_check_slots.acquire() for _ in range(security.MAX_CONCURRENT_PASSWORD_CHECKS)]
    try:
        assert _login(TestClient(app), "alice", "alice-pass-123").status_code == 429
    finally:
        for _ in held:
            security.password_check_slots.release()
    assert _login(TestClient(app), "alice", "alice-pass-123").status_code == 200


def test_job_errors_do_not_leak_tracebacks_or_paths(env) -> None:
    app, tmp_path = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    upload = alice.put(f"/api/projects/{pid}/uploads?filename=broken.xlsx", b"this is not an xlsx")
    job = alice.run_job(pid, "ingest", {"uploads": [upload.json()["upload_id"]], "month": "2026-01"})
    assert job["status"] == "failed"
    text = job["error"]
    assert "Traceback" not in text and str(tmp_path) not in text and ".venv" not in text


def test_successful_job_results_hide_server_paths(env) -> None:
    app, tmp_path = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    upload = alice.put(f"/api/projects/{pid}/uploads?filename=2026-01.csv", CSV.encode("utf-8"))
    job = alice.run_job(pid, "ingest", {"uploads": [upload.json()["upload_id"]]})
    assert job["status"] == "succeeded"
    assert str(tmp_path) not in str(job["result"])


def test_engine_env_is_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-leak")
    monkeypatch.setenv("SELLERSPRITE_CDP_URL", "http://127.0.0.1:9222")
    env = jobs.engine_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["SELLERSPRITE_CDP_URL"] == "http://127.0.0.1:9222" and "PATH" in env


def test_chunked_uploads_cannot_bypass_size_limit(tmp_path) -> None:
    WebStore(tmp_path / "web.db").create_user("alice", "alice-pass-123", "researcher")
    app = create_web_app(db_path=tmp_path / "r.db", root=tmp_path / "p", web_db_path=tmp_path / "web.db",
                         secure_cookies=False, max_upload_bytes=1024)
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)

    def body():
        for _ in range(20):
            yield b"x" * 512  # 无 Content-Length 的分块请求,总计 10KB > 1KB 上限

    resp = alice.client.put(f"/api/projects/{pid}/uploads?filename=big.csv", content=body(),
                            headers={"X-CSRF-Token": alice.csrf})
    assert resp.status_code == 413
    leftovers = list((tmp_path / "p" / pid / "uploads").glob("*"))
    assert leftovers == []  # 超限的临时文件已被清理


def test_chunked_json_body_rejected_and_large_json_rejected(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    chunked = alice.client.post(f"/api/projects/{pid}/jobs", content=iter([b'{"command":"analyze",', b'"params":{}}']),
                                headers={"X-CSRF-Token": alice.csrf, "Content-Type": "application/json"})
    assert chunked.status_code == 411
    huge = alice.client.post(f"/api/projects/{pid}/jobs", content=b"x" * (1024 * 1024 + 1),
                             headers={"X-CSRF-Token": alice.csrf, "Content-Type": "application/json"})
    assert huge.status_code == 413


def test_parameter_collections_are_bounded(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    edits = {f"A{i}": "x" for i in range(2001)}
    resp = alice.post(f"/api/projects/{pid}/jobs",
                      {"command": "resolve", "params": {"checkpoint_id": "cp", "approve": True, "edits": edits}})
    assert resp.status_code == 422


@pytest.mark.parametrize("pid", ["..", "%2E%2E", "../x", "not-a-uuid", "0" * 8])
def test_admin_cannot_traverse_with_malformed_project_id(env, pid) -> None:
    app, _ = env
    admin = Session(app, "root", "root-pass-12345")
    assert admin.put(f"/api/projects/{pid}/uploads?filename=x.csv", b"ASIN\nA\n").status_code in {404, 405}
    assert admin.get(f"/api/projects/{pid}/artifacts/records.json").status_code == 404
    assert admin.get(f"/api/projects/{pid}/dossiers").status_code == 404


def test_admin_unknown_valid_uuid_is_404(env) -> None:
    app, _ = env
    admin = Session(app, "root", "root-pass-12345")
    assert admin.get("/api/projects/11111111-1111-1111-1111-111111111111").status_code == 404


def test_api_docs_and_job_probe_require_auth(env) -> None:
    app, _ = env
    anon = TestClient(app)
    assert anon.get("/docs").status_code == 404 and anon.get("/openapi.json").status_code == 404
    assert anon.get("/api/jobs/does-not-exist").status_code == 401


def test_per_user_active_job_cap(env, tmp_path) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    store = WebStore(tmp_path / "web.db")
    uid = store.get_user_by_name("alice")["id"]
    for i in range(3):
        store.insert_job(project_id=f"other-{i}", command="ingest", params={}, created_by=uid, status="running")
    pid = new_project(alice)
    assert alice.post(f"/api/projects/{pid}/jobs", {"command": "analyze", "params": {}}).status_code == 429


def test_security_headers_present(env, tmp_path) -> None:
    app, _ = env
    headers = TestClient(app).get("/api/me").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in headers  # http 调试模式不发 HSTS
    secure = create_web_app(db_path=tmp_path / "s.db", root=tmp_path / "sp", web_db_path=tmp_path / "web.db",
                            secure_cookies=True)
    assert "strict-transport-security" in TestClient(secure).get("/api/me").headers
