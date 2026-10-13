import asyncio

import pytest

from research_workflow.connectors import ConnectorError, SellerSpriteConnector


class NodeGateway:
    """Fake SellerSprite gateway: rows depend on the requested nodeIdPaths."""

    def __init__(self, fail_nodes=()):
        self.calls = []
        self.fail_nodes = set(fail_nodes)

    async def list_tools(self, server_name):
        return []

    async def call_tool(self, server_name, tool_name, arguments=None):
        arguments = arguments or {}
        self.calls.append(arguments)
        params = arguments["params"]
        nodes = params.get("nodeIdPaths", [])
        if set(nodes) & self.fail_nodes:
            raise RuntimeError("boom")
        node = nodes[0]
        page = params["page"]
        if node == "N-big":  # 101 rows -> needs 2 pages of 100
            items = (
                [{"asin": f"B{i}", "node": node} for i in range(100)]
                if page == 1
                else [{"asin": "BLAST", "node": node}]
            )
            return {"structured_content": {"data": {"items": items, "total": 101}}}
        items = [{"asin": f"{node}-{i}", "node": node} for i in range(3)]
        return {"structured_content": {"items": items, "total": 3}}


def run(coro):
    return asyncio.run(coro)


def test_by_nodes_returns_one_response_per_node_without_mixing():
    g = NodeGateway()
    c = SellerSpriteConnector(g)
    result = run(
        c.product_research_by_nodes(
            marketplace="us", keyword="kw", month="2026-03", node_id_paths_list=["N1", "N2"]
        )
    )
    assert list(result) == ["N1", "N2"]
    for node, resp in result.items():
        assert resp.data["count"] == 3
        assert {r["node"] for r in resp.data["items"]} == {node}
    # each request scoped to exactly one node, month normalised
    assert [call["params"]["nodeIdPaths"] for call in g.calls] == [["N1"], ["N2"]]
    assert g.calls[0]["params"]["month"] == "202603"


def test_by_nodes_keeps_pagination_per_node():
    g = NodeGateway()
    result = run(
        SellerSpriteConnector(g).product_research_by_nodes(
            marketplace="US", keyword="kw", month="2026-03", node_id_paths_list=["N-big"]
        )
    )
    assert result["N-big"].data["count"] == 101
    assert [call["params"]["page"] for call in g.calls] == [1, 2]


def test_by_nodes_dedupes_nodes_and_rejects_empty():
    g = NodeGateway()
    c = SellerSpriteConnector(g)
    result = run(
        c.product_research_by_nodes(
            marketplace="US", keyword="kw", month="2026-03", node_id_paths_list=["N1", "N1"]
        )
    )
    assert list(result) == ["N1"] and len(g.calls) == 1
    with pytest.raises(ValueError):
        run(
            c.product_research_by_nodes(
                marketplace="US", keyword="kw", month="2026-03", node_id_paths_list=[]
            )
        )


def test_by_nodes_failure_propagates_instead_of_partial_result():
    g = NodeGateway(fail_nodes={"N2"})
    c = SellerSpriteConnector(g, retry_attempts=1)
    with pytest.raises(ConnectorError):
        run(
            c.product_research_by_nodes(
                marketplace="US", keyword="kw", month="2026-03", node_id_paths_list=["N1", "N2"]
            )
        )
