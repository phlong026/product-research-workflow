from __future__ import annotations

import re
import math
import unicodedata
from datetime import date
from dataclasses import dataclass
from typing import Any, Iterable

from .models import BSRRecord, DataQualityReport, MonthlyBSRResult, PipelineStats


ALIASES: dict[str, tuple[str, ...]] = {
    "asin": ("ASIN", "asin"),
    "parent_asin": ("父ASIN", "Parent ASIN", "parent_asin", "parentAsin"),
    "brand": ("品牌", "brand", "Brand"),
    "title": ("商品标题", "标题", "title", "Title"),
    "monthly_sales": ("月销量", "销量", "units", "monthly_sales", "monthlySales", "amz_unit"),
    "seller_region": ("卖家所属地", "卖家所在地", "seller_region", "sellerRegion"),
    "node_rank": ("节点排名", "小类BSR", "BSR", "rank", "node_rank"),
    "launch_date": ("上架时间", "上架日期", "launch_date", "launchDate"),
    "image_url": ("商品主图", "图片URL", "image", "image_url"),
}


@dataclass(frozen=True, slots=True)
class MonthlyBSRConfig:
    top_n: int = 100
    merge_cn_hk: bool = True
    dedupe_parent: bool = True
    dedupe_exact_title: bool = True


def _first(row: dict[str, Any], names: tuple[str, ...], default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _to_int(value: Any, default: int | None = 0) -> int | None:
    if value is None or value == "":
        return default
    try:
        number = float(str(value).replace(",", "").strip())
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else default
    except (TypeError, ValueError):
        return default


def normalize_seller_region(value: Any, *, merge_cn_hk: bool = True) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if not raw or raw in {"(空白)", "N/A", "NONE", "NULL"}:
        return "UNKNOWN"
    if merge_cn_hk and raw in {"CN(HK)", "CNHK", "HK", "HONGKONG", "CN-HK"}:
        return "CN"
    if raw.startswith("CN"):
        return "CN" if merge_cn_hk else raw
    if raw in {"USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA"}:
        return "US"
    return raw


def _normal_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = re.sub(r"\s+", " ", text)
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    return text


def parse_bsr_row(row: dict[str, Any], month: str, *, merge_cn_hk: bool = True) -> BSRRecord | None:
    asin = str(_first(row, ALIASES["asin"], "")).strip().upper()
    if not asin:
        return None
    sales = _to_int(_first(row, ALIASES["monthly_sales"], None), None)
    if sales is None:
        return None
    rank = _to_int(_first(row, ALIASES["node_rank"], None), None)
    if rank == 0:
        rank = None
    return BSRRecord(
        asin=asin,
        month=month,
        monthly_sales=sales,
        brand=str(_first(row, ALIASES["brand"], "")).strip(),
        title=str(_first(row, ALIASES["title"], "")).strip(),
        parent_asin=str(_first(row, ALIASES["parent_asin"], "")).strip().upper(),
        seller_region=normalize_seller_region(
            _first(row, ALIASES["seller_region"], ""), merge_cn_hk=merge_cn_hk
        ),
        node_rank=rank,
        launch_date=str(_first(row, ALIASES["launch_date"], "")).strip() or None,
        image_url=str(_first(row, ALIASES["image_url"], "")).strip() or None,
        source_row=row,
    )


def _prefer(a: BSRRecord, b: BSRRecord) -> BSRRecord:
    """Choose the representative row: higher sales, then better rank, then ASIN."""
    a_rank = a.node_rank if a.node_rank is not None else 10**9
    b_rank = b.node_rank if b.node_rank is not None else 10**9
    key_a = (a.monthly_sales, -a_rank, a.asin)
    key_b = (b.monthly_sales, -b_rank, b.asin)
    return a if (-a.monthly_sales, a_rank, a.asin) <= (-b.monthly_sales, b_rank, b.asin) else b


def _dedupe(records: list[BSRRecord], key_fn) -> list[BSRRecord]:
    chosen: dict[str, BSRRecord] = {}
    order: list[str] = []
    for record in records:
        key = key_fn(record)
        if not key:
            key = f"asin:{record.asin}"
        if key not in chosen:
            chosen[key] = record
            order.append(key)
        else:
            chosen[key] = _prefer(chosen[key], record)
    return [chosen[k] for k in order]


def build_monthly_top(
    rows: Iterable[dict[str, Any]],
    month: str,
    config: MonthlyBSRConfig | None = None,
) -> MonthlyBSRResult:
    config = config or MonthlyBSRConfig()
    validate_month(month)
    if config.top_n < 1:
        raise ValueError("top_n must be >= 1")
    source = list(rows)
    missing_asin_rows = sum(1 for row in source if not str(_first(row, ALIASES["asin"], "")).strip())
    invalid_sales_rows = sum(_to_int(_first(row, ALIASES["monthly_sales"], None), None) is None
                             for row in source)
    parsed = [
        record
        for row in source
        if (record := parse_bsr_row(row, month, merge_cn_hk=config.merge_cn_hk)) is not None
    ]
    parent_rows = parsed
    if config.dedupe_parent:
        parent_rows = _dedupe(parsed, lambda r: f"parent:{r.parent_asin}" if r.parent_asin else f"asin:{r.asin}")

    suspicious_rows = parent_rows
    if config.dedupe_exact_title:
        suspicious_rows = _dedupe(
            parent_rows,
            lambda r: (
                f"title:{r.brand.lower().strip()}::{_normal_title(r.title)}"
                if _normal_title(r.title)
                else f"asin:{r.asin}"
            ),
        )

    ordered = sorted(
        suspicious_rows,
        key=lambda r: (-r.monthly_sales, r.node_rank if r.node_rank is not None else 10**9, r.asin),
    )
    output = ordered[: config.top_n]
    return MonthlyBSRResult(
        month=month,
        records=output,
        stats=PipelineStats(
            input_rows=len(source),
            parsed_rows=len(parsed),
            parent_deduped_rows=len(parent_rows),
            suspicious_deduped_rows=len(suspicious_rows),
            output_rows=len(output),
        ),
        quality=DataQualityReport(
            missing_asin_rows=missing_asin_rows,
            invalid_sales_rows=invalid_sales_rows,
            missing_title_rows=sum(1 for r in parsed if not r.title.strip()),
            missing_brand_rows=sum(1 for r in parsed if not r.brand.strip()),
            unknown_seller_region_rows=sum(1 for r in parsed if r.seller_region == "UNKNOWN"),
            missing_rank_rows=sum(1 for r in parsed if r.node_rank is None),
            duplicate_parent_rows_removed=len(parsed) - len(parent_rows),
            duplicate_title_rows_removed=len(parent_rows) - len(suspicious_rows),
        ),
    )


def validate_month(month: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("month must be YYYY-MM")
    date(int(month[:4]), int(month[5:]), 1)
    return month
