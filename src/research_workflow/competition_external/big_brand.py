"""R24 big-brand entry check. ABA threshold is a deterministic rule; meaning is left to a human."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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

DEFAULT_ABA_RANK_THRESHOLD = 20000
STATUS_CANDIDATE = "candidate_big_brand"
STATUS_NOT_CANDIDATE = "not_candidate"
STATUS_UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class BrandAsin:
    asin: str
    listed_date: str | None = None
    rating: float | None = None
    monthly_sales: float | None = None

    def __post_init__(self) -> None:
        require_text(self.asin, "asin")


@dataclass(frozen=True, slots=True)
class BigBrandCheck:
    brand: str
    product_type: str
    status: str
    aba_rank: int | None
    aba_rank_threshold: int
    asin_count: int
    total_monthly_sales: float | None
    sales_missing_count: int
    earliest_asin: str | None
    earliest_listed_date: date | None
    excluded_date_count: int
    evidence: EvidenceRef
    notes: tuple[str, ...]
    human_interpretation_slot: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "brand": self.brand,
            "product_type": self.product_type,
            "status": self.status,
            "aba_rank": self.aba_rank,
            "aba_rank_threshold": self.aba_rank_threshold,
            "asin_count": self.asin_count,
            "total_monthly_sales": self.total_monthly_sales,
            "sales_missing_count": self.sales_missing_count,
            "earliest_asin": self.earliest_asin,
            "earliest_listed_date": self.earliest_listed_date.isoformat() if self.earliest_listed_date else None,
            "excluded_date_count": self.excluded_date_count,
            "evidence": self.evidence.to_dict(),
            "notes": list(self.notes),
            "human_interpretation_slot": self.human_interpretation_slot,
        }
        return guarded(payload, [self.status, *self.notes])


def check_big_brand(
    brand: str,
    product_type: str,
    aba_rank: int | None,
    asins: Iterable[BrandAsin],
    evidence: EvidenceRef,
    *,
    threshold: int = DEFAULT_ABA_RANK_THRESHOLD,
    as_of: date | None = None,
) -> BigBrandCheck:
    """Flag a brand as a candidate big brand when aba_rank <= threshold; ABA missing -> unverified.

    ASIN summary covers every ASIN the brand has in this product type. Dates that are empty,
    unparsable, pre-1995 or after as_of (default: evidence retrieval date) are excluded from the
    earliest-listing lookup and counted.
    """
    require_text(brand, "brand")
    require_text(product_type, "product_type")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if aba_rank is not None and aba_rank <= 0:
        raise ValueError("aba_rank must be a positive integer or None")
    items = list(asins)
    names = [a.asin for a in items]
    if len(set(names)) != len(names):
        raise ValueError("duplicate ASIN in brand ASIN list")

    cutoff = as_of or parse_date(evidence.retrieved_at)
    dated = [(valid_listing_date(a.listed_date, cutoff), a.asin) for a in items]
    valid_dates = sorted((d, asin) for d, asin in dated if d is not None)
    excluded = len(items) - len(valid_dates)
    earliest_date, earliest_asin = valid_dates[0] if valid_dates else (None, None)

    sales = [a.monthly_sales for a in items if a.monthly_sales is not None]
    sales_missing = len(items) - len(sales)

    notes = []
    if aba_rank is None:
        status = STATUS_UNVERIFIED
        notes.append("ABA 排名缺失,未核查,不作判定。")
    elif aba_rank <= threshold:
        status = STATUS_CANDIDATE
        notes.append(f"ABA 排名 {aba_rank} <= 阈值 {threshold},列为候选大品牌(仅阈值规则)。")
    else:
        status = STATUS_NOT_CANDIDATE
        notes.append(f"ABA 排名 {aba_rank} > 阈值 {threshold}。")
    if excluded:
        notes.append(f"{excluded} 个 ASIN 上架日期为空或异常,已排除出最早上架日期计算。")
    if sales_missing:
        notes.append(f"{sales_missing} 个 ASIN 缺月销量,合计销量未含这些 ASIN。")
    notes.append("有无大品牌的业务含义由人工解读(human_interpretation_slot)。")

    return BigBrandCheck(
        brand=brand,
        product_type=product_type,
        status=status,
        aba_rank=aba_rank,
        aba_rank_threshold=threshold,
        asin_count=len(items),
        total_monthly_sales=float(sum(sales)) if sales else None,
        sales_missing_count=sales_missing,
        earliest_asin=earliest_asin,
        earliest_listed_date=earliest_date,
        excluded_date_count=excluded,
        evidence=evidence,
        notes=tuple(notes),
    )


def load_brand_asins(path: str | Path) -> list[BrandAsin]:
    """Import a brand ASIN list from CSV/JSON (asin, listed_date, rating, monthly_sales)."""
    asins = []
    for index, row in enumerate(read_rows(path), start=1):
        try:
            asins.append(
                BrandAsin(
                    asin=row.get("asin") or "",
                    listed_date=clean_text(row.get("listed_date")),
                    rating=optional_float(row.get("rating")),
                    monthly_sales=optional_float(row.get("monthly_sales")),
                )
            )
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc
    return asins
