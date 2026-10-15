"""Web 作业白名单对外部证据命令的支持:import-evidence / build-market-forecast / build-cross-channel。"""
from __future__ import annotations

from external_data import SAMPLES
from test_web_api import CSV, Session, env, fast_hash, new_project  # noqa: F401


def _meta_dict(meta: list[str]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in meta)


def test_import_evidence_job_validates_meta_and_succeeds(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    name, content, meta = SAMPLES["trends"]
    upload = alice.put(f"/api/projects/{pid}/uploads?filename={name}", content.encode("utf-8")).json()["upload_id"]
    job = alice.run_job(pid, "import-evidence", {"kind": "trends", "upload": upload, "meta": _meta_dict(meta)})
    assert job["status"] == "succeeded", job
    assert job["result"]["kind"] == "trends"
    # 缺少必填来源信息 → 引擎报错,作业失败且状态不变
    bad = alice.run_job(pid, "import-evidence", {"kind": "trends", "upload": upload, "meta": {"geo": "US"}})
    assert bad["status"] == "failed" and bad["error"]


def test_import_evidence_rejects_unknown_kind_and_bad_meta(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    post = lambda params: alice.post(f"/api/projects/{pid}/jobs", {"command": "import-evidence", "params": params})
    assert post({"kind": "rm", "upload": "x", "meta": {}}).status_code == 422
    assert post({"kind": "trends", "upload": "nope", "meta": {}}).status_code == 422
    up = alice.put(f"/api/projects/{pid}/uploads?filename=t.csv", b"date,keyword,value\n").json()["upload_id"]
    assert post({"kind": "trends", "upload": up, "meta": {"Bad Key": "x"}}).status_code == 422
    assert post({"kind": "trends", "upload": up, "meta": {"geo": "US\nnewline"}}).status_code == 422
    assert post({"kind": "trends", "upload": up, "meta": {f"k{i}": "v" for i in range(40)}}).status_code == 422


def test_forecast_and_cross_channel_params_are_validated(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    post = lambda command, params: alice.post(f"/api/projects/{pid}/jobs", {"command": command, "params": params})
    assert post("build-market-forecast", {"horizon_years": 0}).status_code == 422
    assert post("build-market-forecast", {"horizon_years": 2, "scope": "everything"}).status_code == 422
    assert post("build-market-forecast", {"horizon_years": 2, "coverage_ratio": 1.5}).status_code == 422
    assert post("build-cross-channel", {"geo": "mars"}).status_code == 422
    # 合法参数可入队(尚无分析结果,作业会因引擎校验而失败,但不是 422)
    job = alice.run_job(pid, "build-market-forecast", {"horizon_years": 2})
    assert job["status"] == "failed" and job["error"]


def test_demand_evidence_kinds_import_through_web(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    for kind in ("keywords", "scenarios", "voc"):
        name, content, meta = SAMPLES[kind]
        upload = alice.put(f"/api/projects/{pid}/uploads?filename={name}", content.encode("utf-8")).json()["upload_id"]
        job = alice.run_job(pid, "import-evidence", {"kind": kind, "upload": upload, "meta": _meta_dict(meta)})
        assert job["status"] == "succeeded", job
        # scope 是必填的取数范围:缺了引擎拒绝,作业失败
        no_scope = {k: v for k, v in _meta_dict(meta).items() if k != "scope"}
        bad = alice.run_job(pid, "import-evidence", {"kind": kind, "upload": upload, "meta": no_scope})
        assert bad["status"] == "failed" and "scope" in bad["error"]


def test_deep_dive_job_params_validated_and_needs_analysis(env) -> None:
    app, _ = env
    alice = Session(app, "alice", "alice-pass-123")
    pid = new_project(alice)
    post = lambda params: alice.post(f"/api/projects/{pid}/jobs", {"command": "deep-dive", "params": params})
    assert post({"modules": ["Bad Name"]}).status_code == 422
    assert post({"modules": ["x"] * 40}).status_code == 422
    assert post({"modules": "seasonality"}).status_code == 422
    assert post({"unexpected": 1}).status_code == 422
    job = alice.run_job(pid, "deep-dive", {"modules": ["seasonality"]})  # 合法但尚无分析结果:引擎拒绝,不是 422
    assert job["status"] == "failed" and "analyze" in job["error"]
