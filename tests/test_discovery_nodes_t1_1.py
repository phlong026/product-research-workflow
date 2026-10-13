import asyncio

from research_workflow.connectors.base import ConnectorResponse
from research_workflow.discovery import (
    discover_nodes,
    merge_nodes_by_asin,
    summarize_nodes,
)


def row(asin, node_id=None, label=None, **extra):
    r = dict(extra)
    if asin is not None:
        r["asin"] = asin
    if node_id is not None:
        r["nodeIdPath"] = node_id
    if label is not None:
        r["nodeLabelPath"] = label
    return r


def test_single_node():
    rows = [row("A1", "100", "Home"), row("A2", "100", "Home")]
    result = summarize_nodes(rows)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert (c.node_id, c.label, c.asin_count, c.share) == ("100", "Home", 2, 1.0)
    assert result.main_node_suggestion == c
    assert result.needs_human_confirmation is True
    assert result.main_node_tied is False


def test_multi_node_sorted_desc_with_share():
    rows = [
        row("A1", "N2", "Two"),
        row("A2", "N1", "One"),
        row("A3", "N1", "One"),
        row("A4", "N1", "One"),
    ]
    result = summarize_nodes(rows)
    assert [c.node_id for c in result.candidates] == ["N1", "N2"]
    assert [c.asin_count for c in result.candidates] == [3, 1]
    assert result.candidates[0].share == 0.75
    assert result.main_node_suggestion.node_id == "N1"
    assert result.needs_human_confirmation is True


def test_same_asin_counted_once_per_node():
    # A1 appears twice under N1 (e.g. repeated rows) and once under N2
    rows = [row("A1", "N1"), row("a1 ", "N1"), row("A1", "N2"), row("A2", "N2")]
    result = summarize_nodes(rows)
    counts = {c.node_id: c.asin_count for c in result.candidates}
    assert counts == {"N1": 1, "N2": 2}
    assert result.sample_asin_count == 2  # distinct ASINs with a node


def test_missing_fields_are_skipped_and_reported():
    rows = [
        row("A1", "N1"),
        row("A2"),  # no node
        row(None, "N1"),  # no asin
        {"asin": "A3", "类目路径": "Home>Kitchen"},  # label alias only -> label is node key
        {"asin": "A4", "nodeId": 555},  # int id coerced
    ]
    result = summarize_nodes(rows)
    assert result.rows_without_node == 1
    assert result.rows_without_asin == 1
    keys = {c.node_id: c for c in result.candidates}
    assert set(keys) == {"N1", "Home>Kitchen", "555"}
    assert keys["Home>Kitchen"].label == "Home>Kitchen"
    assert keys["N1"].label == "N1"  # no label field -> falls back to id


def test_all_rows_unusable_gives_no_suggestion():
    result = summarize_nodes([row("A1"), {}])
    assert result.candidates == ()
    assert result.main_node_suggestion is None
    assert result.needs_human_confirmation is True


def test_tie_for_max_is_stable_and_flagged():
    rows = [row("A1", "N2"), row("A2", "N1")]
    result = summarize_nodes(rows)
    assert [c.node_id for c in result.candidates] == ["N1", "N2"]  # tie -> node_id asc
    assert result.main_node_suggestion.node_id == "N1"
    assert result.main_node_tied is True
    # same output regardless of input order
    assert summarize_nodes(list(reversed(rows))) == result


def test_discover_nodes_uses_connector_and_passes_scope():
    class FakeConnector:
        def __init__(self):
            self.kwargs = None

        async def product_research(self, **kwargs):
            self.kwargs = kwargs
            items = [row("A1", "N1", "One"), row("A2", "N1", "One"), row("A3", "N2", "Two")]
            return ConnectorResponse("sellersprite", "product_research", {"items": items}, [])

    conn = FakeConnector()
    result = asyncio.run(
        discover_nodes(conn, marketplace="US", keyword="kw", month="2026-03")
    )
    assert conn.kwargs["marketplace"] == "US"
    assert conn.kwargs["keyword"] == "kw"
    assert conn.kwargs["month"] == "2026-03"
    assert result.main_node_suggestion.node_id == "N1"
    assert result.needs_human_confirmation is True


def test_merge_dedupes_same_asin_across_nodes():
    merged = merge_nodes_by_asin(
        {
            "N2": [row("A1", "N2", x=2), row("A3", "N2")],
            "N1": [row("A1", "N1", x=1), row("A2", "N1")],
        }
    )
    assert [r["asin"] for r in merged.items] == ["A1", "A2", "A3"]
    # deterministic: nodes processed in sorted key order, so N1 wins A1
    assert merged.items[0]["x"] == 1
    assert merged.nodes_by_asin["A1"] == ("N1", "N2")
    assert merged.nodes_by_asin["A2"] == ("N1",)
    assert merged.node_asin_counts == {"N1": 2, "N2": 2}
    assert merged.node_contribution == {"N1": 2, "N2": 1}
    assert sum(merged.node_contribution.values()) == len(merged.items)


def test_merge_skips_rows_without_asin_and_handles_empty():
    merged = merge_nodes_by_asin({"N1": [{"title": "x"}, row("A1")], "N2": []})
    assert [r["asin"] for r in merged.items] == ["A1"]
    assert merged.rows_without_asin == 1
    assert merged.node_contribution == {"N1": 1, "N2": 0}
    empty = merge_nodes_by_asin({})
    assert empty.items == () and empty.node_contribution == {}


def test_merge_does_not_mutate_input():
    src = {"N1": [row("A1", "N1")]}
    merge_nodes_by_asin(src)
    assert src == {"N1": [row("A1", "N1")]}
