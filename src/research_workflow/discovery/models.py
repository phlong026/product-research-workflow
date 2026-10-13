from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class NodeCandidate:
    """One category node seen in the keyword sample."""

    node_id: str
    label: str
    asin_count: int
    share: float  # asin_count / sample_asin_count (ASINs that carry any node)


@dataclass(slots=True, frozen=True)
class NodeDiscoveryResult:
    candidates: tuple[NodeCandidate, ...]
    # Suggestion only: the main node never takes effect without human confirmation.
    main_node_suggestion: NodeCandidate | None
    needs_human_confirmation: bool
    main_node_tied: bool  # another node has the same ASIN count as the suggestion
    sample_asin_count: int
    rows_without_asin: int
    rows_without_node: int
    # 节点计数基于销量降序的 Top 样本而非全量;sampled=True 表示关键词总量大于样本。
    sampled: bool = False
    sample_rows: int = 0  # 实际取得的样本行数(含无 ASIN/无节点行)


@dataclass(slots=True, frozen=True)
class MergedSample:
    """Multi-node sample merged by ASIN (dispersed products)."""

    items: tuple[dict[str, Any], ...]
    nodes_by_asin: dict[str, tuple[str, ...]]
    node_asin_counts: dict[str, int]  # distinct ASINs each node sampled
    node_contribution: dict[str, int]  # merged rows kept from each node
    rows_without_asin: int = 0


@dataclass(slots=True, frozen=True)
class MonthlyFetchResult:
    """Per-month Top-N candidate pool; missing months are explicit, never zero."""

    months: tuple[str, ...]
    rows_by_month: dict[str, tuple[dict[str, Any], ...]]
    raw_by_month: dict[str, Any]
    requests: dict[str, dict[str, Any]]  # request scope per requested month
    missing_months: dict[str, str] = field(default_factory=dict)  # month -> reason
