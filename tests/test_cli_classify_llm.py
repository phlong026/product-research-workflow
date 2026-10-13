"""classify --provider llm / --enrich 的 CLI 接线(T1-6/T1-7/T1-8)。全部使用假网关与假 LLM。"""
from __future__ import annotations

import json

import pytest

from scripts.run_research import main

TOOLS = [
    "sellersprite_session_status",
    "sellersprite_login",
    "sellersprite_list_operations",
    "sellersprite_describe_operation",
    "sellersprite_call",
]

CSV = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地
E1,90,B1,Nugget Ice Maker Countertop,1,CN
E2,60,B2,Bullet Ice Maker Home,2,
E3,30,B3,Mystery Ice Thing,3,US
"""

DETAILS = {
    "E1": {"asin": "E1", "title": "Nugget Ice Maker Countertop", "sellerRegion": "CN", "bullets": ["Soft nugget ice"]},
    "E2": {"asin": "E2", "title": "Bullet Ice Maker Home", "sellerRegion": "US", "bullets": ["Makes bullet ice fast"]},
    "E3": {"asin": "E3", "title": "Mystery Ice Thing", "sellerRegion": "US", "bullets": []},
}


class FakeGateway:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []

    async def list_tools(self, server_name: str) -> list[str]:
        return sorted(TOOLS)

    async def call_tool(self, server_name, tool_name, arguments):
        if tool_name == "sellersprite_session_status":
            return {"structured_content": {"loggedIn": True}}
        assert arguments["operation"] == "asin_detail"
        asin = arguments["params"]["asin"]
        self.detail_calls.append(asin)
        return {"structured_content": DETAILS[asin]}


async def fake_complete(prompt: str) -> str:
    products = json.loads(prompt.split("Products: ", 1)[1].split("\n", 1)[0])
    rows = []
    for p in products:
        text = " ".join([p["title"], *p["bullets"]])
        if "Nugget" in text:
            rows.append({"asin": p["asin"], "label": "nugget", "confidence": "high", "evidence": "Nugget"})
        elif "bullet ice" in text:
            rows.append({"asin": p["asin"], "label": "bullet", "confidence": "high", "evidence": "bullet ice"})
        else:
            rows.append({"asin": p["asin"], "label": "nugget", "confidence": "low", "evidence": "Mystery",
                         "ambiguous": True})
    return json.dumps(rows)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import scripts.run_research as rr

    gw = FakeGateway()
    monkeypatch.setattr(rr, "_build_gateway", lambda config_path: gw)
    monkeypatch.setattr(rr, "_build_llm_complete", lambda: fake_complete)
    (tmp_path / "2026-01.csv").write_text(CSV, encoding="utf-8")
    return gw


def _run(tmp_path, capsys, *argv):
    capsys.readouterr()
    main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
    return json.loads(capsys.readouterr().out)


def _prepare(tmp_path, capsys):
    pid = _run(tmp_path, capsys, "init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = _run(tmp_path, capsys, "ingest", "--project", pid, "--file", str(tmp_path / "2026-01.csv"))
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    return pid


def test_llm_classify_with_enrichment_blocks_approval_until_review_resolved(env, tmp_path, capsys) -> None:
    pid = _prepare(tmp_path, capsys)
    # 补齐是独立步骤,须先经补齐检查点人工确认才能分类
    enr = _run(tmp_path, capsys, "enrich", "--project", pid)
    assert enr["checkpoint_step"] == "enrichment_review"
    assert enr["enrichment"]["unknown_region_before"] == 1 and enr["enrichment"]["unknown_region_after"] == 0
    with pytest.raises(SystemExit):  # 补齐未确认:不能分类
        _run(tmp_path, capsys, "classify", "--project", pid, "--provider", "llm",
             "--label", "nugget=Nugget", "--label", "bullet=Bullet")
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", enr["checkpoint_id"], "--approve")

    out = _run(tmp_path, capsys, "classify", "--project", pid, "--provider", "llm",
               "--label", "nugget=Nugget", "--label", "bullet=Bullet")
    assert out["classification_provider"] == "llm"
    assert out["enrichment"]["unknown_region_before"] == 1 and out["enrichment"]["unknown_region_after"] == 0
    assert out["needs_review"] == ["E3"]
    assert out["label_counts"]["nugget"] == 1 and out["label_counts"]["bullet"] == 1
    pdir = tmp_path / "projects" / pid
    enrichment = json.loads((pdir / "enrichment.json").read_text(encoding="utf-8"))
    assert enrichment["regions"]["E2"] == "US" and enrichment["bullets"]["E2"] == ["Makes bullet ice fast"]
    # 地区缺失的 E2 才会取详情做地区补齐;卖点补齐按唯一 ASIN 只取一次
    assert env.detail_calls.count("E2") <= 2

    # 仍有待人工行 → 不能直接批准
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    # 人工指定 E3 后可批准
    done = _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", out["checkpoint_id"],
                "--approve", "--edit", "E3=bullet")
    assert done["resolution"] == "approved"

    # analyze 使用补齐后的地区,且上下文如实记录 provider
    _run(tmp_path, capsys, "analyze", "--project", pid)
    ctx = json.loads((pdir / "analysis_context.json").read_text(encoding="utf-8"))
    assert ctx["classification_provider"] == "llm" and ctx["enrichment"] == "done"
    assert '"UNKNOWN"' not in (pdir / "market_metrics.json").read_text(encoding="utf-8")


def test_classify_enrich_flag_is_retired_in_favour_of_enrich_command(env, tmp_path, capsys) -> None:
    pid = _prepare(tmp_path, capsys)
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "classify", "--project", pid, "--enrich", "--label", "nugget=Nugget")
    assert env.detail_calls == []  # 没有悄悄做补齐


def test_enrichment_review_gates_downstream_and_reject_points_back_to_enrich(env, tmp_path, capsys) -> None:
    pid = _prepare(tmp_path, capsys)
    enr = _run(tmp_path, capsys, "enrich", "--project", pid)
    status = _run(tmp_path, capsys, "status", "--project", pid)
    assert status["pending_checkpoint"]["step_key"] == "enrichment_review"
    assert status["pending_checkpoint"]["payload"]["summary"]["regions_filled"] >= 1
    rej = _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", enr["checkpoint_id"], "--reject")
    assert rej["next"] == "enrich"
    assert _run(tmp_path, capsys, "status", "--project", pid)["next"] == "enrich"
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=Nugget", "--rule", "nugget=nugget")
    # 重新补齐 → 新检查点 → 确认后才能分类
    enr2 = _run(tmp_path, capsys, "enrich", "--project", pid)
    assert enr2["checkpoint_id"] != enr["checkpoint_id"]
    assert _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", enr2["checkpoint_id"],
                "--approve")["next"] == "classify"
    out = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=Nugget", "--rule", "nugget=nugget")
    assert out["enrichment"]["regions_filled"] >= 1


def test_re_enrich_after_classification_invalidates_approvals_and_downstream(env, tmp_path, capsys) -> None:
    pid = _prepare(tmp_path, capsys)
    enr = _run(tmp_path, capsys, "enrich", "--project", pid)
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", enr["checkpoint_id"], "--approve")
    cls = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=Nugget", "--rule", "nugget=nugget")
    _run(tmp_path, capsys, "resolve", "--project", pid, "--checkpoint", cls["checkpoint_id"], "--approve",
         "--edit", "E3=nugget", "--edit", "E2=nugget")
    _run(tmp_path, capsys, "enrich", "--project", pid)  # 重新补齐:旧确认与分类全部作废
    pdir = tmp_path / "projects" / pid
    assert not (pdir / "classification.json").exists()
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, "analyze", "--project", pid)


def test_default_rules_provider_unchanged_and_enrichment_skipped(env, tmp_path, capsys) -> None:
    pid = _prepare(tmp_path, capsys)
    out = _run(tmp_path, capsys, "classify", "--project", pid, "--label", "nugget=Nugget",
               "--label", "bullet=Bullet", "--rule", "nugget=nugget", "--rule", "bullet=bullet")
    assert out["classification_provider"] == "rules" and out["enrichment"] == "skipped"
    assert env.detail_calls == []
