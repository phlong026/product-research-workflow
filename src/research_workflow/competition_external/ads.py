"""R23 ad / traffic / review-rate / CPC ledger for ASINs chosen by a human (never auto-picked)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger import (
    EvidenceRef,
    clean_text,
    evidence_from_row,
    guarded,
    optional_float,
    read_rows,
    require_text,
)

COHORTS = ("head_waist_tail", "new_product")
REVIEW_WINDOW_MONTHS = 12


@dataclass(frozen=True, slots=True)
class MonthlyReviewSales:
    month: str  # e.g. "2026-03"
    new_reviews: float | None
    sales: float | None


@dataclass(frozen=True, slots=True)
class ReviewRateResult:
    """Deterministic review rate = sum(new reviews) / sum(sales). None when not computable."""

    rate: float | None
    new_reviews_total: float | None
    sales_total: float | None
    month_count: int
    monthly_rates: dict[str, float | None]
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "new_reviews_total": self.new_reviews_total,
            "sales_total": self.sales_total,
            "month_count": self.month_count,
            "monthly_rates": dict(self.monthly_rates),
            "note": self.note,
        }


def compute_review_rate_12m(months: Iterable[MonthlyReviewSales]) -> ReviewRateResult:
    """12-month review rate. Missing months / missing values / zero denominator -> None + note.

    A missing value is never treated as 0.
    """
    items = list(months)
    names = [m.month for m in items]
    if len(set(names)) != len(names):
        raise ValueError("duplicate months in review series")
    if len(items) > REVIEW_WINDOW_MONTHS:
        raise ValueError(f"review series has {len(items)} months, expected at most {REVIEW_WINDOW_MONTHS}")
    monthly = {
        m.month: (m.new_reviews / m.sales if m.new_reviews is not None and m.sales else None)
        for m in items
    }
    if len(items) < REVIEW_WINDOW_MONTHS:
        note = f"缺月: 仅提供 {len(items)}/{REVIEW_WINDOW_MONTHS} 个月,未计算"
        return ReviewRateResult(None, None, None, len(items), monthly, note)
    bad = [m.month for m in items if m.new_reviews is None or m.sales is None]
    if bad:
        note = f"缺值: {', '.join(bad)} 的评价数或销量缺失,未计算"
        return ReviewRateResult(None, None, None, len(items), monthly, note)
    reviews_total = sum(m.new_reviews for m in items)
    sales_total = sum(m.sales for m in items)
    if sales_total == 0:
        return ReviewRateResult(None, reviews_total, sales_total, len(items), monthly, "分母为0: 近12个月销量合计为0,未计算")
    return ReviewRateResult(reviews_total / sales_total, reviews_total, sales_total, len(items), monthly, None)


def _require_time_range(value: str, block: str) -> str:
    return require_text(value, f"{block}.time_range")


@dataclass(frozen=True, slots=True)
class TrafficStructure:
    """Ad architecture / traffic mix, recorded exactly as the caller provided it."""

    time_range: str
    evidence: EvidenceRef
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_time_range(self.time_range, "traffic")

    def to_dict(self) -> dict[str, Any]:
        return {"time_range": self.time_range, "data": dict(self.data), "evidence": self.evidence.to_dict()}


@dataclass(frozen=True, slots=True)
class ReviewSeries:
    time_range: str
    evidence: EvidenceRef
    months: tuple[MonthlyReviewSales, ...] = ()

    def __post_init__(self) -> None:
        _require_time_range(self.time_range, "reviews")

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_range": self.time_range,
            "months": [
                {"month": m.month, "new_reviews": m.new_reviews, "sales": m.sales} for m in self.months
            ],
            "review_rate_12m": compute_review_rate_12m(self.months).to_dict(),
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CpcRecord:
    """3-month CPC from Helium10 or a screenshot (screenshot refs need file path + SHA256)."""

    time_range: str
    evidence: EvidenceRef
    value: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _require_time_range(self.time_range, "cpc")
        if self.evidence.file_path and not self.evidence.sha256:
            raise ValueError("CPC screenshot evidence requires sha256 together with file_path")
        if self.value is not None and self.value < 0:
            raise ValueError("cpc value must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_range": self.time_range,
            "value": self.value,
            "currency": self.currency,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AdRecord:
    """Evidence bundle for one human-selected ASIN."""

    asin: str
    cohort: str
    selected_by: str
    traffic: TrafficStructure | None = None
    reviews: ReviewSeries | None = None
    cpc: CpcRecord | None = None

    def __post_init__(self) -> None:
        require_text(self.asin, "asin")
        require_text(self.selected_by, "selected_by")
        if self.cohort not in COHORTS:
            raise ValueError(f"cohort must be one of {COHORTS}, got {self.cohort!r}")

    def to_dict(self) -> dict[str, Any]:
        notes = ["ASIN 由人工指定,系统不自动挑选。"]
        missing = [
            name
            for name, block in (("traffic", self.traffic), ("reviews", self.reviews), ("cpc", self.cpc))
            if block is None
        ]
        if missing:
            notes.append(f"未登记: {', '.join(missing)}")
        reviews_dict = self.reviews.to_dict() if self.reviews else None
        if reviews_dict and reviews_dict["review_rate_12m"]["note"]:
            notes.append(f"留评率: {reviews_dict['review_rate_12m']['note']}")
        payload = {
            "asin": self.asin,
            "cohort": self.cohort,
            "selected_by": self.selected_by,
            "traffic": self.traffic.to_dict() if self.traffic else None,
            "reviews": reviews_dict,
            "cpc": self.cpc.to_dict() if self.cpc else None,
            "notes": notes,
        }
        return guarded(payload, notes)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value) if value.strip() else None
    return value


def _block_evidence(row: Mapping[str, Any], index: int, prefix: str) -> EvidenceRef:
    """Block-specific source columns (e.g. cpc_source_name) override row-level ones."""
    if clean_text(row.get(f"{prefix}source_name")) or clean_text(row.get(f"{prefix}file_path")):
        return evidence_from_row(row, row_no=index, prefix=prefix)
    return evidence_from_row(row, row_no=index)


def load_ad_records(path: str | Path) -> list[AdRecord]:
    """Import AdRecords from CSV/JSON (one row per ASIN).

    Columns: asin, cohort, selected_by, source_name/url/file_path/sha256/retrieved_at (row level,
    overridable per block via traffic_/reviews_/cpc_ prefixes), traffic_time_range, traffic_data
    (JSON object), reviews_time_range, review_months (JSON list of {month,new_reviews,sales}),
    cpc_time_range, cpc_value, cpc_currency.
    """
    records = []
    for index, row in enumerate(read_rows(path), start=1):
        try:
            traffic = reviews = cpc = None
            traffic_data = _json_value(row.get("traffic_data"))
            if clean_text(row.get("traffic_time_range")) or traffic_data:
                traffic = TrafficStructure(
                    row.get("traffic_time_range") or "",
                    _block_evidence(row, index, "traffic_"),
                    traffic_data or {},
                )
            review_months = _json_value(row.get("review_months"))
            if clean_text(row.get("reviews_time_range")) or review_months:
                reviews = ReviewSeries(
                    row.get("reviews_time_range") or "",
                    _block_evidence(row, index, "reviews_"),
                    tuple(
                        MonthlyReviewSales(
                            str(item["month"]),
                            optional_float(item.get("new_reviews")),
                            optional_float(item.get("sales")),
                        )
                        for item in (review_months or [])
                    ),
                )
            if clean_text(row.get("cpc_time_range")) or clean_text(row.get("cpc_value")):
                cpc = CpcRecord(
                    row.get("cpc_time_range") or "",
                    _block_evidence(row, index, "cpc_"),
                    optional_float(row.get("cpc_value")),
                    clean_text(row.get("cpc_currency")),
                )
            records.append(
                AdRecord(
                    asin=row.get("asin") or "",
                    cohort=clean_text(row.get("cohort")) or "",
                    selected_by=row.get("selected_by") or "",
                    traffic=traffic,
                    reviews=reviews,
                    cpc=cpc,
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            raise ValueError(message if message.startswith("row ") else f"row {index}: {message}") from exc
    return records
