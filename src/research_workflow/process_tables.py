"""三张过程表（供人工逐阶段检查、修改、上传）。

表1 = 清洗后的月度 Top 商品（数据检查点）
表2 = 商品补齐明细：卖点、卖家所属地等（补齐阶段）
表3 = 商品分类（分类检查点）

表头统一用中文，且都能被 bsr_pipeline / enrichment 的别名表读回，保证“导出 → 人工改 → 上传”闭环。
"""
from __future__ import annotations

import re
from typing import Any

from research_workflow.data.bsr_pipeline import _to_float, _to_int, normalize_seller_region, validate_month
from research_workflow.data.models import BSRRecord
from research_workflow.enrichment.models import ASINProfile

T1_COLUMNS = ["月份", "ASIN", "父ASIN", "品牌", "商品标题", "月销量", "节点排名", "卖家", "卖家所属地",
              "评分", "评分数", "上架时间", "商品主图", "节点"]


def table1_rows(records: list[BSRRecord]) -> list[dict[str, Any]]:
    return [
        {"月份": r.month, "ASIN": r.asin, "父ASIN": r.parent_asin, "品牌": r.brand, "商品标题": r.title,
         "月销量": r.monthly_sales, "节点排名": r.node_rank, "卖家": r.seller, "卖家所属地": r.seller_region,
         "评分": r.rating, "评分数": r.rating_count, "上架时间": r.launch_date, "商品主图": r.image_url,
         "节点": ",".join(r.node_ids)}
        for r in sorted(records, key=lambda r: (r.month, -r.monthly_sales, r.asin))
    ]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_table1_rows(rows: list[dict[str, Any]]) -> dict[str, list[BSRRecord]]:
    """校验并读回人工修改后的表1。缺失销量不得当 0；(月份, ASIN) 不得重复。"""
    by_month: dict[str, list[BSRRecord]] = {}
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=2):  # 表头为第 1 行
        month = _text(row.get("月份"))
        if re.fullmatch(r"\d{4}-\d{2}", month) is None:
            raise ValueError(f"第 {index} 行月份必须为 YYYY-MM: {month!r}")
        validate_month(month)
        asin = _text(row.get("ASIN")).upper()
        if not asin:
            raise ValueError(f"第 {index} 行缺少 ASIN")
        if (month, asin) in seen:
            raise ValueError(f"第 {index} 行重复: {month} {asin}")
        seen.add((month, asin))
        sales = _to_int(row.get("月销量"), None)
        if sales is None:
            raise ValueError(f"第 {index} 行月销量缺失或非法（不能视为 0）: {row.get('月销量')!r}")
        rank = _to_int(row.get("节点排名"), None)
        rating = _to_float(row.get("评分"))
        by_month.setdefault(month, []).append(BSRRecord(
            asin=asin, month=month, monthly_sales=sales,
            brand=_text(row.get("品牌")), title=_text(row.get("商品标题")),
            parent_asin=_text(row.get("父ASIN")).upper(),
            seller_region=normalize_seller_region(row.get("卖家所属地")),
            node_rank=rank or None, launch_date=_text(row.get("上架时间")) or None,
            image_url=_text(row.get("商品主图")) or None, seller=_text(row.get("卖家")),
            rating=rating, rating_count=_to_int(row.get("评分数"), None),
            node_ids=[n for n in _text(row.get("节点")).split(",") if n],
        ))
    return by_month


def table2_rows(profiles: dict[str, ASINProfile]) -> list[dict[str, Any]]:
    return [
        {"ASIN": p.asin, "标题": p.title, "品牌": p.brand, "卖点": "\n".join(p.bullets),
         "卖家所属地": p.seller_region, "上架时间": p.launch_date, "价格": p.price, "评分": p.rating,
         "评论数": p.review_count, "来源": ",".join(p.sources)}
        for _, p in sorted(profiles.items())
    ]


def table3_rows(rows: dict[str, Any], label_names: dict[str, str], titles: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"ASIN": asin, "标题": titles.get(asin, ""), "分类": row.label,
         "分类名称": label_names.get(row.label, ""), "置信度": row.confidence,
         "依据": row.rationale, "来源": row.source}
        for asin, row in sorted(rows.items())
    ]
