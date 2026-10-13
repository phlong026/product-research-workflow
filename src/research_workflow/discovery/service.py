"""Category node discovery (T1-1).

NOTE: the row field names below are INFERRED, not verified against the real
SellerSprite ``product_research`` schema -- 字段名为推断,待真实 schema 验证.
They are centralised in the ``*_FIELDS`` constants so a verified schema only
needs one edit here.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from ..connectors.base import ConnectorResponse
from .models import MergedSample, NodeCandidate, NodeDiscoveryResult

ASIN_FIELDS: tuple[str, ...] = ("asin", "ASIN", "Asin")
NODE_ID_FIELDS: tuple[str, ...] = ("nodeIdPath", "nodeId", "node_id_path", "node_id")
NODE_LABEL_FIELDS: tuple[str, ...] = (
    "nodeLabelPath",
    "categoryPath",
    "node_label_path",
    "category_path",
    "类目路径",
    "节点",
)


class ProductResearchSource(Protocol):
    async def product_research(self, **kwargs: Any) -> ConnectorResponse: ...


def _first_text(row: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    """Texts of the first alias present with a non-empty value (list values expand)."""
    for name in fields:
        value = row.get(name)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        texts = [str(v).strip() for v in values if v is not None and str(v).strip()]
        if texts:
            return texts
    return []


def _asin_of(row: Mapping[str, Any]) -> str | None:
    texts = _first_text(row, ASIN_FIELDS)
    return texts[0].upper() if texts else None


def _nodes_of(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(node_id, label) pairs; falls back to the label when no id field exists."""
    ids = _first_text(row, NODE_ID_FIELDS)
    labels = _first_text(row, NODE_LABEL_FIELDS)
    if not ids:
        return [(label, label) for label in labels]
    # Pair labels with ids only when the pairing is unambiguous; else label = id.
    if len(labels) != len(ids):
        labels = ids
    return list(zip(ids, labels))


def summarize_nodes(rows: Iterable[Mapping[str, Any]]) -> NodeDiscoveryResult:
    """Count distinct sample ASINs per category node (pure, deterministic)."""
    asins_by_node: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    sample_asins: set[str] = set()
    no_asin = no_node = 0
    for row in rows:
        asin = _asin_of(row)
        if asin is None:
            no_asin += 1
            continue
        nodes = _nodes_of(row)
        if not nodes:
            no_node += 1
            continue
        sample_asins.add(asin)
        for node_id, label in nodes:
            asins_by_node.setdefault(node_id, set()).add(asin)
            labels.setdefault(node_id, label)

    total = len(sample_asins)
    ordered = sorted(asins_by_node.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    candidates = tuple(
        NodeCandidate(node_id, labels[node_id], len(asins), len(asins) / total)
        for node_id, asins in ordered
    )
    top = candidates[0] if candidates else None
    tied = len(candidates) > 1 and candidates[1].asin_count == candidates[0].asin_count
    return NodeDiscoveryResult(
        candidates=candidates,
        main_node_suggestion=top,
        needs_human_confirmation=True,  # main node must be confirmed by a human
        main_node_tied=tied,
        sample_asin_count=total,
        rows_without_asin=no_asin,
        rows_without_node=no_node,
    )


async def discover_nodes(
    connector: ProductResearchSource,
    *,
    marketplace: str,
    keyword: str,
    month: str | None = None,
    page_size: int = 100,
    max_pages: int = 5,
) -> NodeDiscoveryResult:
    """Sample a keyword through the connector and rank candidate category nodes."""
    response = await connector.product_research(
        marketplace=marketplace,
        keyword=keyword,
        month=month,
        page_size=page_size,
        max_pages=max_pages,
    )
    items = response.data.get("items", []) if isinstance(response.data, dict) else []
    return summarize_nodes(items)


def merge_nodes_by_asin(rows_by_node: Mapping[str, list[dict[str, Any]]]) -> MergedSample:
    """Merge per-node samples by ASIN for dispersed products.

    Nodes are processed in sorted key order and the first row seen for an ASIN
    is kept, so output is independent of dict insertion order.
    """
    kept: dict[str, dict[str, Any]] = {}
    seen_nodes: dict[str, list[str]] = {}
    owner: dict[str, str] = {}
    asin_counts: dict[str, int] = {}
    no_asin = 0
    for node in sorted(rows_by_node):
        node_asins: set[str] = set()
        for row in rows_by_node[node]:
            asin = _asin_of(row)
            if asin is None:
                no_asin += 1
                continue
            node_asins.add(asin)
            if asin not in kept:
                kept[asin] = dict(row)
                owner[asin] = node
            if node not in seen_nodes.setdefault(asin, []):
                seen_nodes[asin].append(node)
        asin_counts[node] = len(node_asins)
    contribution = {node: 0 for node in sorted(rows_by_node)}
    for node in owner.values():
        contribution[node] += 1
    return MergedSample(
        items=tuple(kept.values()),
        nodes_by_asin={a: tuple(n) for a, n in seen_nodes.items()},
        node_asin_counts=asin_counts,
        node_contribution=contribution,
        rows_without_asin=no_asin,
    )
