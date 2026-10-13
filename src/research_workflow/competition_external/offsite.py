"""R28 off-site capability evidence ledger. Facts only; the seller-maturity call stays human."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .ledger import (
    EvidenceRef,
    clean_text,
    guarded,
    optional_float,
    parse_date,
    read_rows,
    require_text,
    valid_listing_date,
)

OFFSITE_STATUSES = ("found", "not_found", "not_searched")
NOT_FOUND_NOTE = "未找到≠不存在:仅表示在所列引擎/工具与 query 下未检索到,不代表该品牌没有站外活动。"
NOT_SEARCHED_NOTE = "未检索:尚未执行该检索,不可视为未找到。"
HUMAN_NOTE = "卖家成色(已推爆/待推爆/持续上新)由人工判断(human_judgement_slot),此处仅汇总事实。"
_STATUS_LABELS = {"found": "已找到", "not_found": "未找到", "not_searched": "未检索"}


@dataclass(frozen=True, slots=True)
class OffsiteFinding:
    channel_type: str  # e.g. social, deal site, influencer, forum
    url: str
    discovered_at: str

    def __post_init__(self) -> None:
        require_text(self.channel_type, "channel_type")
        require_text(self.url, "url")
        require_text(self.discovered_at, "discovered_at")

    def to_dict(self) -> dict[str, Any]:
        return {"channel_type": self.channel_type, "url": self.url, "discovered_at": self.discovered_at}


@dataclass(frozen=True, slots=True)
class OffsiteSearch:
    """One brand + product-term search on one engine/tool."""

    brand: str
    product_term: str
    query: str
    engine_tool: str
    status: str
    searched_at: str | None = None
    findings: tuple[OffsiteFinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("brand", "product_term", "query", "engine_tool"):
            require_text(getattr(self, name), name)
        if self.status not in OFFSITE_STATUSES:
            raise ValueError(f"status must be one of {OFFSITE_STATUSES}, got {self.status!r}")
        if self.status != "not_searched" and not clean_text(self.searched_at):
            raise ValueError("searched_at is required when the search was performed")
        if self.status == "found" and not self.findings:
            raise ValueError("found requires at least one OffsiteFinding")
        if self.status != "found" and self.findings:
            raise ValueError(f"{self.status} must not carry findings")

    def to_dict(self) -> dict[str, Any]:
        label = _STATUS_LABELS[self.status]
        notes = []
        if self.status == "not_found":
            notes.append(NOT_FOUND_NOTE)
        if self.status == "not_searched":
            notes.append(NOT_SEARCHED_NOTE)
        payload = {
            "brand": self.brand,
            "product_term": self.product_term,
            "query": self.query,
            "engine_tool": self.engine_tool,
            "status": self.status,
            "status_label": label,
            "searched_at": self.searched_at,
            "findings": [f.to_dict() for f in self.findings],
            "notes": notes,
        }
        return guarded(payload, [label, *notes])


@dataclass(frozen=True, slots=True)
class CrossCategoryAsin:
    asin: str
    category: str
    listed_date: str | None = None
    monthly_sales: float | None = None

    def __post_init__(self) -> None:
        require_text(self.asin, "asin")
        require_text(self.category, "category")


def summarize_cross_category(
    asins: Iterable[CrossCategoryAsin], evidence: EvidenceRef, *, as_of: date | None = None
) -> dict[str, Any]:
    """Per-category facts for the same brand: counts, sales, listing-date range and listings per year.

    No thresholds or labels: whether the seller looks 'proven', 'about to be pushed' or 'launching
    continuously' is a human call.
    """
    items = list(asins)
    names = [a.asin for a in items]
    if len(set(names)) != len(names):
        raise ValueError("duplicate ASIN in cross-category list")
    cutoff = as_of or parse_date(evidence.retrieved_at)

    grouped: dict[str, list[CrossCategoryAsin]] = defaultdict(list)
    for item in items:
        grouped[item.category].append(item)

    categories = []
    for category in sorted(grouped):
        rows = grouped[category]
        dates = [d for d in (valid_listing_date(r.listed_date, cutoff) for r in rows) if d is not None]
        sales = [r.monthly_sales for r in rows if r.monthly_sales is not None]
        by_year: dict[str, int] = defaultdict(int)
        for d in dates:
            by_year[str(d.year)] += 1
        categories.append(
            {
                "category": category,
                "asin_count": len(rows),
                "total_monthly_sales": float(sum(sales)) if sales else None,
                "sales_missing_count": len(rows) - len(sales),
                "asins_with_sales": sum(1 for s in sales if s > 0),
                "earliest_listed_date": min(dates).isoformat() if dates else None,
                "latest_listed_date": max(dates).isoformat() if dates else None,
                "excluded_date_count": len(rows) - len(dates),
                "listings_by_year": dict(sorted(by_year.items())),
            }
        )
    return {
        "category_count": len(categories),
        "asin_count": len(items),
        "categories": categories,
        "evidence": evidence.to_dict(),
        "notes": ["销量合计仅含有销量数据的 ASIN;上架日期为空或异常的 ASIN 已排除出日期统计并计数。"],
    }


@dataclass(frozen=True, slots=True)
class OffsiteLedger:
    brand: str
    searches: tuple[OffsiteSearch, ...]
    cross_category_asins: tuple[CrossCategoryAsin, ...] = ()
    cross_category_evidence: EvidenceRef | None = None
    human_judgement_slot: str = ""

    def __post_init__(self) -> None:
        require_text(self.brand, "brand")
        for search in self.searches:
            if search.brand.strip().lower() != self.brand.strip().lower():
                raise ValueError(f"search brand {search.brand!r} does not match ledger brand {self.brand!r}")
        if self.cross_category_asins and self.cross_category_evidence is None:
            raise ValueError("cross_category_evidence is required with a cross-category ASIN list")

    def to_dict(self, *, as_of: date | None = None) -> dict[str, Any]:
        cross = (
            summarize_cross_category(self.cross_category_asins, self.cross_category_evidence, as_of=as_of)
            if self.cross_category_asins
            else None
        )
        counts = {s: sum(1 for x in self.searches if x.status == s) for s in OFFSITE_STATUSES}
        notes = [NOT_FOUND_NOTE, HUMAN_NOTE]
        if counts["not_searched"]:
            notes.append(NOT_SEARCHED_NOTE)
        payload = {
            "brand": self.brand,
            "status_counts": counts,
            "searches": [s.to_dict() for s in self.searches],
            "cross_category": cross,
            "notes": notes,
            "human_judgement_slot": self.human_judgement_slot,
        }
        return guarded(payload, notes)


def load_offsite_searches(path: str | Path) -> list[OffsiteSearch]:
    """Import searches from CSV/JSON; rows sharing brand/term/query/engine/time/status form one search.

    Columns: brand, product_term, query, engine_tool, searched_at, status, and for found rows
    channel_type, url, discovered_at.
    """
    groups: dict[tuple, list[OffsiteFinding]] = {}
    for index, row in enumerate(read_rows(path), start=1):
        key = tuple(
            clean_text(row.get(k)) or ""
            for k in ("brand", "product_term", "query", "engine_tool", "searched_at", "status")
        )
        findings = groups.setdefault(key, [])
        if clean_text(row.get("url")) or clean_text(row.get("channel_type")):
            try:
                findings.append(
                    OffsiteFinding(row.get("channel_type") or "", row.get("url") or "", row.get("discovered_at") or "")
                )
            except ValueError as exc:
                raise ValueError(f"row {index}: {exc}") from exc
    searches = []
    for (brand, term, query, engine, searched_at, status), findings in groups.items():
        try:
            searches.append(
                OffsiteSearch(brand, term, query, engine, status, searched_at or None, tuple(findings))
            )
        except ValueError as exc:
            raise ValueError(f"search {query!r}: {exc}") from exc
    return searches


def load_cross_category_asins(path: str | Path) -> list[CrossCategoryAsin]:
    """Import same-brand ASINs with categories (asin, category, listed_date, monthly_sales)."""
    asins = []
    for index, row in enumerate(read_rows(path), start=1):
        try:
            asins.append(
                CrossCategoryAsin(
                    asin=row.get("asin") or "",
                    category=row.get("category") or "",
                    listed_date=clean_text(row.get("listed_date")),
                    monthly_sales=optional_float(row.get("monthly_sales")),
                )
            )
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc
    return asins
