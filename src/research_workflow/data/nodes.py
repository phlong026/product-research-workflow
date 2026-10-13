"""类目节点发现与多节点合并。

先用产品关键词取样 -> 统计各节点销量/商品数 -> 提议主节点与相关节点 -> 人工确认 ->
按确认的节点集合分别取数，再合并候选池做去重与 TopN。

注意：节点字段别名（NODE_ID_ALIASES/NODE_LABEL_ALIASES）按卖家精灵常见字段预置，
**尚未用真实接口返回验证**，真实联调时需对照 raw 快照确认。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .bsr_pipeline import ALIASES, _first, _to_int

NODE_ID_ALIASES = ("nodeIdPath", "node_id_path", "节点ID路径", "nodeId", "categoryId", "类目节点ID")
NODE_LABEL_ALIASES = ("nodeLabelPath", "node_label_path", "类目路径", "categoryPath", "节点路径", "bsrCategory")
NODE_TAG = "_node_ids"


@dataclass(slots=True, frozen=True)
class NodeCandidate:
    node_id: str
    label: str
    asin_count: int
    total_sales: int
    sales_share_pct: float
    asin_share_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class NodeProposal:
    """节点提议：只是给人看的建议，必须经人工确认后才用于取数。"""

    primary: NodeCandidate | None
    related: list[NodeCandidate]
    others: list[NodeCandidate]
    sampled_rows: int
    rows_without_node: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict() if self.primary else None,
            "related": [c.to_dict() for c in self.related],
            "others": [c.to_dict() for c in self.others],
            "sampled_rows": self.sampled_rows,
            "rows_without_node": self.rows_without_node,
        }


def discover_nodes(
    rows: Iterable[dict[str, Any]],
    *,
    min_sales_share_pct: float = 5.0,
    min_asin_share_pct: float = 10.0,
) -> NodeProposal:
    """从关键词取样结果中统计节点分布，提议主节点与相关节点。

    主节点=销量最大的节点；相关节点=销量占比或商品数占比达到阈值的其余节点。
    没有节点字段的行不归入任何节点，单独计数（rows_without_node），不猜测。
    """
    sales: dict[str, int] = defaultdict(int)
    asins: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    total_rows = 0
    without_node = 0
    for row in rows:
        total_rows += 1
        node_id = str(_first(row, NODE_ID_ALIASES, "")).strip()
        if not node_id:
            without_node += 1
            continue
        labels.setdefault(node_id, str(_first(row, NODE_LABEL_ALIASES, "")).strip())
        value = _to_int(_first(row, ALIASES["monthly_sales"], None), None)
        asin = str(_first(row, ALIASES["asin"], "")).strip().upper()
        if value is not None:
            sales[node_id] += value
        if asin:
            asins[node_id].add(asin)
        else:
            asins[node_id].add(f"<row:{total_rows}>")
    node_ids = set(sales) | set(asins)
    total_sales = sum(sales.values())
    total_asins = sum(len(v) for v in asins.values())
    candidates = [
        NodeCandidate(
            node_id=node_id,
            label=labels.get(node_id, ""),
            asin_count=len(asins[node_id]),
            total_sales=sales[node_id],
            sales_share_pct=(sales[node_id] / total_sales * 100.0) if total_sales else 0.0,
            asin_share_pct=(len(asins[node_id]) / total_asins * 100.0) if total_asins else 0.0,
        )
        for node_id in node_ids
    ]
    candidates.sort(key=lambda c: (-c.total_sales, -c.asin_count, c.node_id))
    if not candidates:
        return NodeProposal(None, [], [], total_rows, without_node)
    primary, rest = candidates[0], candidates[1:]
    related = [
        c for c in rest
        if c.sales_share_pct >= min_sales_share_pct or c.asin_share_pct >= min_asin_share_pct
    ]
    others = [c for c in rest if c not in related]
    return NodeProposal(primary, related, others, total_rows, without_node)


def merge_node_rows(rows_by_node: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """合并多个节点的候选行。同一 ASIN 出现在多个节点只保留一行（销量最高者），
    并在 `_node_ids` 中记录它出现过的全部节点，保证多节点合并后不重复计销量。
    """
    chosen: dict[str, dict[str, Any]] = {}
    seen_nodes: dict[str, list[str]] = defaultdict(list)
    anonymous: list[dict[str, Any]] = []
    for node_id in sorted(rows_by_node):
        for row in rows_by_node[node_id]:
            asin = str(_first(row, ALIASES["asin"], "")).strip().upper()
            if not asin:
                anonymous.append({**row, NODE_TAG: [node_id]})
                continue
            if node_id not in seen_nodes[asin]:
                seen_nodes[asin].append(node_id)
            sales = _to_int(_first(row, ALIASES["monthly_sales"], None), None)
            current = chosen.get(asin)
            current_sales = (
                _to_int(_first(current, ALIASES["monthly_sales"], None), None) if current else None
            )
            if current is None or (sales or -1) > (current_sales or -1):
                chosen[asin] = row
    merged = [{**row, NODE_TAG: list(seen_nodes[asin])} for asin, row in chosen.items()]
    return merged + anonymous
