import asyncio

import pytest

from research_workflow.connectors import (
    ApifyConnector,
    ConnectorError,
    SellerSpriteConnector,
    XiyouConnector,
    route_for,
)


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.toolsets = {
            "sellersprite": [
                "sellersprite_session_status",
                "sellersprite_login",
                "sellersprite_list_operations",
                "sellersprite_describe_operation",
                "sellersprite_call",
            ],
            "xiyou": list(XiyouConnector.KNOWN_TOOLS),
            "apify": ["search-actors", "call-actor"],
        }

    async def list_tools(self, server_name):
        return self.toolsets.get(server_name, [])

    async def call_tool(self, server_name, tool_name, arguments=None):
        self.calls.append((server_name, tool_name, arguments or {}))
        return {"structured_content": {"ok": True, "tool": tool_name, "args": arguments or {}}}


def run(coro):
    return asyncio.run(coro)


def test_sellersprite_stable_tool_flow():
    g = FakeGateway()
    c = SellerSpriteConnector(g)
    tools = run(c.validate())
    assert "sellersprite_call" in tools
    result = run(c.call_operation("product_research", {"marketplace": "US", "size": 100}))
    assert result.data["ok"] is True
    assert g.calls[-1][1] == "sellersprite_call"
    assert g.calls[-1][2]["operation"] == "product_research"


def test_sellersprite_validation_fails_when_server_shape_changes():
    g = FakeGateway()
    g.toolsets["sellersprite"] = ["sellersprite_call"]
    with pytest.raises(ConnectorError, match="missing required tools"):
        run(SellerSpriteConnector(g).validate())


def test_apify_wait_is_bounded_to_official_limit():
    g = FakeGateway()
    run(ApifyConnector(g).call_actor("apify/rag-web-browser", {"query": "ice maker"}, wait_secs=100))
    assert g.calls[-1][2]["waitSecs"] == 45


def test_capability_routing_is_vendor_replaceable():
    market = route_for("amazon_market_concentration")
    assert market.primary_provider == "sellersprite"
    assert "sif" in market.fallback_providers

class PagingGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.page_attempts = {}

    async def call_tool(self, server_name, tool_name, arguments=None):
        arguments = arguments or {}
        self.calls.append((server_name, tool_name, arguments))
        if tool_name != "sellersprite_call":
            return {"structured_content": {"ok": True}}
        op = arguments["operation"]
        params = arguments["params"]
        if op == "product_research":
            page = params["page"]
            key = (op, page)
            self.page_attempts[key] = self.page_attempts.get(key, 0) + 1
            if page == 1 and self.page_attempts[key] == 1:
                raise RuntimeError("temporary")
            items = (
                [{"asin": f"A{i}"} for i in range(100)]
                if page == 1
                else [{"asin": "LAST"}]
            )
            return {"structured_content": {"data": {"items": items, "total": 101}}}
        if op == "review":
            page = params["page"]
            items = [{"content": str(i)} for i in range(10)] if page == 1 else []
            return {"structured_content": {"items": items}}
        if op == "asin_detail":
            return {"structured_content": {"asin": params["asin"], "brand": "X"}}
        return {"structured_content": {"items": []}}


def test_sellersprite_product_research_has_retry_and_bounded_pagination():
    g = PagingGateway()
    c = SellerSpriteConnector(g, retry_attempts=2)
    result = run(c.product_research(marketplace="US", keyword="ice maker", page_size=100))
    assert result.data["count"] == 101
    assert result.data["items"][-1]["asin"] == "LAST"
    assert g.page_attempts[("product_research", 1)] == 2
    calls = [x for x in g.calls if x[1] == "sellersprite_call" and x[2]["operation"] == "product_research"]
    assert calls[-1][2]["params"]["page"] == 2


def test_sellersprite_business_methods_hide_operation_envelopes():
    g = PagingGateway()
    c = SellerSpriteConnector(g)
    detail = run(c.asin_detail(marketplace="US", asin="b012345678"))
    assert detail.data["asin"] == "B012345678"
    reviews = run(c.reviews(marketplace="US", asin="b012345678", max_pages=2))
    assert reviews.data["count"] == 10
