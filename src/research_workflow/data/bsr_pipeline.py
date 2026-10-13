from __future__ import annotations

import re
import math
import unicodedata
from datetime import date
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Literal

from .models import BSRRecord, DataQualityReport, DedupeAuditEntry, MonthlyBSRResult, PipelineStats


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
    "seller_id": ("卖家ID", "sellerId", "seller_id", "Seller ID"),
    # 小写 "ratings" 在不同数据源里既可能是星级也可能是评分数,两边都登记,由取值范围裁决(见 _to_rating)
    "rating": ("评分", "Rating", "rating", "星级", "ratings"),
    "rating_count": ("评分数", "Ratings", "reviews", "ratingCount", "rating_count", "ratings"),
}

CrossParentRule = Literal["sales_rating_reviews", "brand_title", "none"]
_CROSS_PARENT_RULES = ("sales_rating_reviews", "brand_title", "none")


@dataclass(frozen=True, slots=True)
class MonthlyBSRConfig:
    top_n: int = 100
    merge_cn_hk: bool = True
    dedupe_parent: bool = True
    # 父体之间的重复判定:
    #   sales_rating_reviews(默认,原需求):月销量+评分+评分数三值完全相同(任一缺失不判重)
    #   brand_title(legacy):品牌+规范化标题相同
    #   none:不做跨父体去重
    cross_parent_rule: CrossParentRule = "sales_rating_reviews"


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


def _to_rating(value: Any) -> float | None:
    """星级只接受 (0, 5];0、越界或非数字一律视为缺失(0 星在亚马逊等价于无评分)。"""
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number <= 5 else None


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
        seller_id=str(_first(row, ALIASES["seller_id"], "")).strip(),
        rating=_to_rating(_first(row, ALIASES["rating"], None)),
        rating_count=_to_int(_first(row, ALIASES["rating_count"], None), None),
        source_row=row,
    )


def _prefer(a: BSRRecord, b: BSRRecord) -> BSRRecord:
    """Choose the representative row: higher sales, then better rank, then ASIN."""
    a_rank = a.node_rank if a.node_rank is not None else 10**9
    b_rank = b.node_rank if b.node_rank is not None else 10**9
    key_a = (a.monthly_sales, -a_rank, a.asin)
    key_b = (b.monthly_sales, -b_rank, b.asin)
    return a if (-a.monthly_sales, a_rank, a.asin) <= (-b.monthly_sales, b_rank, b.asin) else b


def _dedupe(
    records: list[BSRRecord],
    key_fn: Callable[[BSRRecord], str],
    *,
    month: str,
    rule: str,
    reason: Callable[[BSRRecord], str],
) -> tuple[list[BSRRecord], list[DedupeAuditEntry]]:
    """Collapse records sharing a key into one representative (via _prefer) and audit every removal."""
    groups: dict[str, list[BSRRecord]] = {}
    for record in records:
        groups.setdefault(key_fn(record) or f"asin:{record.asin}", []).append(record)
    kept: list[BSRRecord] = []
    audit: list[DedupeAuditEntry] = []
    for key, members in groups.items():
        representative = members[0]
        for member in members[1:]:
            representative = _prefer(representative, member)
        kept.append(representative)
        is_same_asin = key.startswith("asin:")
        for member in members:
            if member is representative:
                continue
            audit.append(
                DedupeAuditEntry(
                    month=month,
                    rule="asin_duplicate" if is_same_asin else rule,
                    kept_asin=representative.asin,
                    removed_asin=member.asin,
                    reason="同 ASIN 重复行,保留销量更高/名次更优者" if is_same_asin else reason(member),
                )
            )
    audit.sort(key=lambda e: (e.kept_asin, e.removed_asin))
    return kept, audit


def _triple_key(record: BSRRecord) -> str:
    """月销量+评分+评分数 三值键;任一缺失(或为 0,0 不构成重复证据)返回空串,即不判重。"""
    if (
        record.monthly_sales <= 0
        or record.rating is None
        or record.rating_count is None
        or record.rating_count <= 0
    ):
        return ""
    return f"srr:{record.monthly_sales}:{record.rating!r}:{record.rating_count}"


def _brand_title_key(record: BSRRecord) -> str:
    title = _normal_title(record.title)
    return f"title:{record.brand.lower().strip()}::{title}" if title else ""


def build_monthly_top(
    rows: Iterable[dict[str, Any]],
    month: str,
    config: MonthlyBSRConfig | None = None,
) -> MonthlyBSRResult:
    config = config or MonthlyBSRConfig()
    validate_month(month)
    if config.top_n < 1:
        raise ValueError("top_n must be >= 1")
    if config.cross_parent_rule not in _CROSS_PARENT_RULES:
        raise ValueError(f"cross_parent_rule must be one of {_CROSS_PARENT_RULES}")
    source = list(rows)
    missing_asin_rows = sum(1 for row in source if not str(_first(row, ALIASES["asin"], "")).strip())
    invalid_sales_rows = sum(_to_int(_first(row, ALIASES["monthly_sales"], None), None) is None
                             for row in source)
    parsed = [
        record
        for row in source
        if (record := parse_bsr_row(row, month, merge_cn_hk=config.merge_cn_hk)) is not None
    ]
    audit: list[DedupeAuditEntry] = []
    parent_rows = parsed
    if config.dedupe_parent:
        parent_rows, parent_audit = _dedupe(
            parsed,
            lambda r: f"parent:{r.parent_asin}" if r.parent_asin else f"asin:{r.asin}",
            month=month,
            rule="parent_asin",
            reason=lambda r: f"同父ASIN {r.parent_asin} 的变体,折叠为销量更高/名次更优的代表行",
        )
        audit += parent_audit

    suspicious_rows = parent_rows
    if config.cross_parent_rule == "sales_rating_reviews":
        suspicious_rows, cross_audit = _dedupe(
            parent_rows,
            _triple_key,
            month=month,
            rule="sales_rating_reviews",
            reason=lambda r: (
                f"父体不同但月销量={r.monthly_sales}、评分={r.rating}、评分数={r.rating_count} 完全相同,视为同一商品"
            ),
        )
        audit += cross_audit
    elif config.cross_parent_rule == "brand_title":
        suspicious_rows, cross_audit = _dedupe(
            parent_rows,
            _brand_title_key,
            month=month,
            rule="brand_title",
            reason=lambda r: f"品牌 {r.brand!r} 与规范化标题相同(legacy 规则)",
        )
        audit += cross_audit

    ordered = sorted(
        suspicious_rows,
        key=lambda r: (-r.monthly_sales, r.node_rank if r.node_rank is not None else 10**9, r.asin),
    )
    # sales_rank 是清洗后按销量重排的连续名次;node_rank 保留源 BSR 名次,不足 top_n 不补齐
    output = [replace(r, sales_rank=i) for i, r in enumerate(ordered[: config.top_n], start=1)]
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
            # 字段名沿用旧口径;现为"跨父体去重(当前 cross_parent_rule)删除的行数"
            duplicate_title_rows_removed=len(parent_rows) - len(suspicious_rows),
        ),
        dedupe_audit=audit,
    )


def validate_month(month: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("month must be YYYY-MM")
    date(int(month[:4]), int(month[5:]), 1)
    return month
