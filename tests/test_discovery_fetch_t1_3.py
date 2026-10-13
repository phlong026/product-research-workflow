import asyncio

import pytest

from research_workflow.connectors import SellerSpriteConnector
from research_workflow.discovery import fetch_monthly_top


class MonthGateway:
    """Fake SellerSprite gateway keyed by month; supports empty and failing months."""

    def __init__(self, empty=(), failing=(), rows_per_month=250):
        self.calls = []
        self.empty = set(empty)
        self.failing = set(failing)
        self.rows_per_month = rows_per_month

    async def list_tools(self, server_name):
        return []

    async def call_tool(self, server_name, tool_name, arguments=None):
        arguments = arguments or {}
        self.calls.append(arguments)
        params = arguments["params"]
        month = params["month"]
        if month in self.failing:
            raise RuntimeError("upstream 500")
        if month in self.empty:
            return {"structured_content": {"items": [], "total": 0}}
        start = (params["page"] - 1) * params["size"]
        end = min(start + params["size"], self.rows_per_month)
        # server already returns amz_unit desc
        items = [
            {"asin": f"{month}-{i:04d}", "amz_unit": 10000 - i} for i in range(start, end)
        ]
        return {"structured_content": {"data": {"items": items, "total": self.rows_per_month}}}


def run(coro):
    return asyncio.run(coro)


def fetch(gateway, months, **kw):
    conn = SellerSpriteConnector(gateway, retry_attempts=1)
    return run(fetch_monthly_top(conn, marketplace="us", keyword="kw", months=months, **kw))


def test_all_months_success_top200_by_sales():
    g = MonthGateway()
    result = fetch(g, ["2026-01", "2026-02"])
    assert result.months == ("2026-01", "2026-02")
    assert result.missing_months == {}
    for month in result.months:
        rows = result.rows_by_month[month]
        assert len(rows) == 200
        sales = [r["amz_unit"] for r in rows]
        assert sales == sorted(sales, reverse=True)
        assert result.raw_by_month[month]  # raw responses kept
        req = result.requests[month]
        assert req["marketplace"] == "US" and req["keyword"] == "kw" and req["top_n"] == 200
        assert req["month"] == month
    # no wasted pages: 200 rows = 2 pages per month
    assert len(g.calls) == 4


def test_rows_resorted_by_sales_when_server_order_differs():
    class Unsorted(MonthGateway):
        async def call_tool(self, server_name, tool_name, arguments=None):
            return {
                "structured_content": {
                    "items": [
                        {"asin": "A", "amz_unit": 5},
                        {"asin": "B", "amz_unit": 9},
                        {"asin": "C"},  # missing sales sorts last
                        {"asin": "D", "amz_unit": 9},  # tie -> original order kept
                    ]
                }
            }

    result = fetch(Unsorted(), ["2026-01"], top_n=4)
    assert [r["asin"] for r in result.rows_by_month["2026-01"]] == ["B", "D", "A", "C"]


def test_empty_month_recorded_as_missing_not_zero():
    result = fetch(MonthGateway(empty={"202602"}), ["2026-01", "2026-02", "2026-03"])
    assert set(result.missing_months) == {"2026-02"}
    assert "empty" in result.missing_months["2026-02"]
    assert "2026-02" not in result.rows_by_month
    assert len(result.rows_by_month["2026-01"]) == 200
    assert result.requests["2026-02"]["month"] == "2026-02"  # scope still recorded


def test_connector_error_month_recorded_with_reason():
    result = fetch(MonthGateway(failing={"202602"}), ["2026-01", "2026-02"])
    assert set(result.missing_months) == {"2026-02"}
    reason = result.missing_months["2026-02"]
    assert reason.startswith("connector_error") and "RuntimeError" in reason
    assert "2026-02" not in result.raw_by_month
    assert "2026-01" in result.rows_by_month


def test_node_scope_is_passed_through_and_recorded():
    g = MonthGateway(rows_per_month=5)
    result = fetch(g, ["2026-01"], node_id_paths=["N1"], top_n=5)
    assert g.calls[0]["params"]["nodeIdPaths"] == ["N1"]
    assert result.requests["2026-01"]["node_id_paths"] == ["N1"]


def test_top_n_beyond_page_budget_raises():
    with pytest.raises(ValueError):
        fetch(MonthGateway(), ["2026-01"], top_n=201, page_size=100, max_pages=2)


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        fetch(MonthGateway(), [])
    with pytest.raises(ValueError):
        fetch(MonthGateway(), ["2026/01"])
    with pytest.raises(ValueError):
        fetch(MonthGateway(), ["2026-01"], top_n=0)


def test_duplicate_months_fetched_once():
    g = MonthGateway()
    result = fetch(g, ["2026-01", "2026-01"], top_n=100)
    assert result.months == ("2026-01",)
    assert len(g.calls) == 1
