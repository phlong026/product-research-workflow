"""T5 历史知识库(R01):归档只收已完成且分类已确认的项目;检索/历史标签/ASIN 对照只作参考。"""
from __future__ import annotations

import sqlite3

import pytest

from research_workflow.knowledge import KnowledgeBase, build_payload, tokenize
from tests.external_data import DEMAND_META, KEYWORDS_CSV, Cli, analyzed_project


def _payload(project_id: str = "p1", product: str = "Ice Maker", marketplace: str = "US", labels=None, asins=None, notes=None):
    labels = labels or [("nugget", "Nugget 冰"), ("bullet", "Bullet 冰")]
    asins = asins or {"B0AAA": "nugget", "B0BBB": "bullet"}
    envelope = {
        "schema": {"dimension": "ice_type", "unknown_label": "unknown",
                   "labels": [{"key": k, "name": n, "description": "", "examples": []} for k, n in labels]},
        "batch": {"schema_hash": "h", "version": "v1", "approved": True, "human_edits": {"B0BBB": "bullet"},
                  "rows": {a: {"label": lab, "confidence": "high"} for a, lab in asins.items()}},
        "provider": "rules",
    }
    return build_payload(
        project={"project_id": project_id, "product": product, "marketplace": marketplace, "primary_keyword": None,
                 "main_node": None, "research_start": None, "research_end": None},
        classification_envelope=envelope, market={"market": [{"month": "2026-01", "total_sales": 100}]}, competition=None,
        new_products=None, context={"new_products_skipped_reason": "未设置推起阈值"}, approvals=[], external_index=[],
        deep_dive=None, sources={}, human_notes=notes or [],
    )


@pytest.fixture()
def kb(tmp_path) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / "kb" / "knowledge.db")


def test_tokenize_handles_english_digits_and_chinese_bigrams() -> None:
    assert tokenize("Ice Maker 2-in-1") == ["ice", "maker", "2", "in", "1"]
    assert tokenize("制冰机") == ["制冰机", "制冰", "冰机"]


def test_archive_is_idempotent_and_versions_changes(kb) -> None:
    first = kb.archive(_payload(), "2026-10-01T00:00:00Z")
    assert first["status"] == "archived" and first["version"] == 1
    again = kb.archive(_payload(), "2026-10-02T00:00:00Z")
    assert again["status"] == "unchanged" and again["archive_id"] == first["archive_id"]
    changed = kb.archive(_payload(notes=["口径:留存率分子只看当期"]), "2026-10-03T00:00:00Z")
    assert changed["status"] == "archived" and changed["version"] == 2
    # 检索只返回每个项目的最新版本
    results = kb.search("ice maker")
    assert len(results) == 1 and results[0]["human_notes"] == ["口径:留存率分子只看当期"]


def test_search_ranks_by_matched_terms_then_recency_and_filters_marketplace(kb) -> None:
    bullet = [("bullet", "Bullet")]
    kb.archive(_payload("p1", "Ice Maker", "US", labels=bullet, asins={"B0AAA": "bullet"}), "2026-10-01T00:00:00Z")
    kb.archive(_payload("p2", "Nugget Ice Maker Pro", "US", labels=[("nugget", "Nugget")]), "2026-10-02T00:00:00Z")
    kb.archive(_payload("p3", "Ice Maker", "DE", labels=bullet, asins={"B0AAA": "bullet"}), "2026-10-03T00:00:00Z")
    kb.archive(_payload("p4", "制冰机", "US"), "2026-10-04T00:00:00Z")
    ids = [r["project_id"] for r in kb.search("nugget ice maker")]
    # p2 命中 3 词;p3/p1 命中 2 词,按归档时间倒序;p4 只因默认标签含 nugget 命中 1 词
    assert ids == ["p2", "p3", "p1", "p4"]
    assert [r["project_id"] for r in kb.search("ice maker", marketplace="DE")] == ["p3"]
    assert [r["project_id"] for r in kb.search("制冰")] == ["p4"]
    assert kb.search("refrigerator") == []
    with pytest.raises(ValueError, match="为空"):
        kb.search("   ")


def test_label_suggestions_are_reference_only_with_counts(kb) -> None:
    kb.archive(_payload(), "2026-10-01T00:00:00Z")
    suggestion = kb.label_suggestions("ice maker")[0]
    assert suggestion["from_project"] == "p1" and "classification_review" in suggestion["note"]
    counts = {label["key"]: label["asin_count"] for label in suggestion["labels"]}
    assert counts == {"nugget": 1, "bullet": 1}


def test_asin_lookup_reports_history_and_unseen(kb) -> None:
    kb.archive(_payload("p1"), "2026-10-01T00:00:00Z")
    kb.archive(_payload("p2", asins={"B0AAA": "bullet"}), "2026-10-02T00:00:00Z")
    found = kb.lookup_asins(["b0aaa", "B0ZZZ"])
    assert [(r["project_id"], r["label"]) for r in found["B0AAA"]] == [("p2", "bullet"), ("p1", "nugget")]
    assert found["B0ZZZ"] == []
    with pytest.raises(ValueError):
        kb.lookup_asins([" "])


def test_search_is_parameterised_not_injectable(kb) -> None:
    kb.archive(_payload(), "2026-10-01T00:00:00Z")
    assert kb.search("x') OR 1=1 --") == []
    with sqlite3.connect(kb.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1


# ------------------------------------------------------------------ CLI


@pytest.fixture()
def cli(tmp_path, capsys) -> Cli:
    cli = Cli(tmp_path, capsys)
    cli.base += ["--kb", str(tmp_path / "kb.db")]
    return cli


def test_archive_requires_completed_project(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)  # 还没出 dossier
    assert "已完成" in cli.fail("kb-archive", "--project", pid)["error"]


def test_archive_search_labels_lookup_end_to_end(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)
    cli.import_file(pid, "keywords", cli.write("kw.csv", KEYWORDS_CSV), DEMAND_META)
    cli.run("deep-dive", "--project", pid, "--modules", "keyword_research,voc_research")
    cli.run("dossier", "--project", pid, "--output", str(cli.tmp_path / "d"))

    out = cli.run("kb-archive", "--project", pid, "--note", "口径:留存率分子只看当期", "--note", "  ")
    assert out["status"] == "archived" and "不生成商业结论" in out["note"]
    assert cli.run("kb-archive", "--project", pid, "--note", "口径:留存率分子只看当期")["status"] == "unchanged"

    shown = cli.run("kb-show", "--project", pid)["payload"]
    assert shown["human_notes"] == ["口径:留存率分子只看当期"] and shown["project"]["product"] == "Ice Maker"
    assert shown["classification"]["approved"] is True and shown["metrics"].endswith("(--full 查看)")
    assert {"data_review", "classification_review"} <= {x["step"] for x in shown["approvals"]}
    assert all(x["status"] == "approved" for x in shown["approvals"])
    assert shown["deep_dive"]["modules"] == ["keyword_research", "voc_research"]
    assert any("用户评论" in slot for slot in shown["deep_dive"]["missing_slots"])  # 缺口一并归档,不丢
    assert [e["kind"] for e in shown["external"]] == ["keywords"]
    full = cli.run("kb-show", "--project", pid, "--full")["payload"]
    assert {r["asin"] for r in full["classification"]["asin_labels"]} == {"N1", "B1"} and full["metrics"]

    assert cli.run("kb-search", "--query", "ice maker")["results"][0]["project_id"] == pid
    assert cli.run("kb-search", "--query", "fridge")["count"] == 0
    labels = cli.run("kb-labels", "--query", "Ice Maker")
    assert {label["key"] for label in labels["suggestions"][0]["labels"]} == {"nugget", "bullet"}
    looked = cli.run("kb-lookup", "--asin", "n1", "--asin", "NOPE")
    assert looked["unseen"] == ["NOPE"] and looked["asins"]["N1"][0]["label"] == "nugget"


def test_archive_never_stores_credentials_or_decisions(cli) -> None:
    pid = analyzed_project(cli, months=24, confirm_route=True)
    cli.run("dossier", "--project", pid, "--output", str(cli.tmp_path / "d"))
    cli.run("kb-archive", "--project", pid)
    raw = (cli.tmp_path / "kb.db").read_bytes()
    for needle in (b"secret-key", b"Bearer", b"YES", "最终结论".encode()):
        assert needle not in raw


def test_kb_show_requires_a_selector_and_reports_unknown(cli) -> None:
    assert "--project" in cli.fail("kb-show")["error"]
    assert cli.fail("kb-show", "--project", "nope")["error"]
