"""商品补齐 → LLM 分类 的主流程接通（假 Claude 客户端，无需密钥）。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from research_workflow.classification import ClassificationLabel, ClassificationSchema
from research_workflow.classification.llm import AnthropicClassificationProvider
from research_workflow.enrichment.models import ASINProfile
from scripts.run_research import main


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if reply == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[], usage=SimpleNamespace(input_tokens=10, output_tokens=0))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=reply)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def _client(replies):
    return SimpleNamespace(messages=FakeMessages(replies))


SCHEMA = ClassificationSchema("p", "segment", [ClassificationLabel("nugget", "咀嚼冰"), ClassificationLabel("bullet", "子弹冰")])


def _profiles(*asins):
    return [ASINProfile(a, title=f"t{a}", bullets=["b"]) for a in asins]


def test_llm_provider_batches_parses_fences_and_tracks_usage():
    client = _client([
        '```json\n[{"asin":"A","label":"nugget","confidence":"high","rationale":"r"}]\n```',
        'noise [{"asin":"B","label":"bullet","confidence":"low","rationale":"r"}] tail',
    ])
    provider = AnthropicClassificationProvider(model="m", client=client, batch_size=1)
    rows = asyncio.run(provider.classify(SCHEMA, _profiles("A", "B")))
    assert {r["asin"]: r["label"] for r in rows} == {"A": "nugget", "B": "bullet"}
    assert provider.usage.requests == 2 and provider.usage.input_tokens == 200
    assert client.messages.calls[0]["model"] == "m"
    assert "thinking" not in client.messages.calls[0] and "temperature" not in client.messages.calls[0]


def test_llm_provider_refusal_and_garbage_leave_asins_for_human():
    client = _client(["refusal", "not json", "still not json"])
    provider = AnthropicClassificationProvider(client=client, batch_size=1)
    rows = asyncio.run(provider.classify(SCHEMA, _profiles("A", "B")))
    assert rows == []
    assert provider.usage.failed_asins == ["A", "B"] and provider.usage.refusals == 1


def test_llm_provider_drops_rows_for_asins_not_requested():
    client = _client(['[{"asin":"ZZZ","label":"nugget"},{"asin":"A","label":"nugget"}]'])
    rows = asyncio.run(AnthropicClassificationProvider(client=client).classify(SCHEMA, _profiles("A")))
    assert [r["asin"] for r in rows] == ["A"]


@pytest.fixture()
def cli(tmp_path, capsys):
    def run(*argv):
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    def fail(*argv):
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)["error"]

    run.fail = fail
    run.tmp = tmp_path
    return run


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _ingested(cli):
    pid = cli("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    f = _w(cli.tmp, "2026-01.csv", "ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地\nN1,100,NB,Nugget Maker,1,\nB1,50,BB,Bullet Maker,2,US\n")
    out = cli("ingest", "--project", pid, "--file", f)
    cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    return pid


DETAILS = "ASIN,标题,卖点,卖家所属地\nN1,Nugget Maker,\"soft chewable nugget ice\",CN(HK)\nB1,Bullet Maker,\"bullet shaped ice\",US\n"


def test_enrich_fills_bullets_and_seller_region_and_feeds_llm_classification(cli, monkeypatch):
    import scripts.run_research as rr

    pid = _ingested(cli)
    out = cli.fail("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--provider", "llm")
    assert "enrich" in out

    details = _w(cli.tmp, "details.csv", DETAILS)
    out = cli("enrich", "--project", pid, "--details-file", details)
    assert out["coverage"]["with_bullets"] == 2 and out["coverage"]["seller_region_filled_asins"] == 1

    fake = _client(['[{"asin":"N1","label":"nugget","confidence":"high","rationale":"x"},'
                    '{"asin":"B1","label":"bullet","confidence":"high","rationale":"y"}]'])
    monkeypatch.setattr(rr, "_build_llm_provider",
                        lambda args: AnthropicClassificationProvider(model="fake", client=fake))
    out = cli("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--label", "bullet=子弹冰", "--provider", "llm")
    assert out["enrichment"] == "completed" and out["classification_provider"] == "anthropic:fake"
    assert out["label_counts"] == {"nugget": 1, "bullet": 1}
    assert out["llm"]["usage"]["requests"] == 1
    # 卖点确实进入了发给模型的提示
    assert "soft chewable nugget ice" in fake.messages.calls[0]["messages"][0]["content"]

    out = cli("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    out = cli("analyze", "--project", pid)
    pdir = cli.tmp / "projects" / pid
    ctx = json.loads((pdir / "analysis_context.json").read_text(encoding="utf-8"))
    assert ctx["enrichment"] == "completed" and ctx["classification_provider"] == "anthropic:fake"
    market = json.loads((pdir / "market_metrics.json").read_text(encoding="utf-8"))
    regions = {r["seller_region"] for r in market["seller_regions"]}
    assert "UNKNOWN" not in regions and "CN" in regions  # N1 的 CN(HK) 已补齐并归并为 CN


def test_stale_profiles_are_never_used_after_reingest(cli):
    pid = _ingested(cli)
    cli("enrich", "--project", pid, "--details-file", _w(cli.tmp, "details.csv", DETAILS))
    pdir = cli.tmp / "projects" / pid
    assert (pdir / "profiles.json").exists()
    f = _w(cli.tmp, "2026-01b.csv", "ASIN,月销量,品牌,商品标题\nN1,120,NB,Nugget Maker\n")
    cli("ingest", "--project", pid, "--file", f, "--month", "2026-01")
    assert not (pdir / "profiles.json").exists()


def test_rules_classification_without_enrichment_reports_skipped(cli):
    pid = _ingested(cli)
    out = cli("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--rule", "nugget=nugget")
    assert out["enrichment"] == "skipped" and out["classification_provider"] == "rules"


def test_llm_requires_bullet_coverage(cli, monkeypatch):
    import scripts.run_research as rr

    pid = _ingested(cli)
    cli("enrich", "--project", pid, "--details-file", _w(cli.tmp, "d.csv", "ASIN,标题\nN1,Nugget Maker\nB1,Bullet Maker\n"))
    monkeypatch.setattr(rr, "_build_llm_provider", lambda args: AnthropicClassificationProvider(client=_client([])))
    assert "卖点覆盖率" in cli.fail("classify", "--project", pid, "--label", "nugget=咀嚼冰", "--provider", "llm")
