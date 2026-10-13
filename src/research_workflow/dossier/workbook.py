"""综合 Excel 成品：原始数据 + 表1/2/3 + 统计分析 + 研究发现，供人工继续编辑。

只含研究证据；“人工决策”栏恒为空（红线 1）。所有数值为静态值（无公式），缺失=空单元格。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from research_workflow.data.xlsx import write_xlsx

from .models import ResearchDossierInput


def _rows(items: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [asdict(x) if is_dataclass(x) else dict(x) for x in (items or [])]


def build_workbook_sheets(
    dossier: ResearchDossierInput, tables: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    tables = tables or {}
    sheets: dict[str, list[dict[str, Any]]] = {}
    index = [
        ("说明", "项目信息、工作表索引、缺失证据"),
        ("原始数据", "各月各节点取回的原始候选行（清洗前）"),
        ("表1_清洗Top", "清洗去重并截取 TopN 后的月度商品"),
        ("表2_商品补齐", "补齐的卖点、卖家所属地等"),
        ("表3_分类", "商品分类及依据"),
    ]
    sheets["说明"] = [
        {"项目": "项目ID", "内容": dossier.project_id},
        {"项目": "产品", "内容": dossier.product},
        {"项目": "站点", "内容": dossier.marketplace},
        {"项目": "人工决策（Yes/No）", "内容": ""},
        {"项目": "说明", "内容": "本工作簿只含研究证据；最终商业决策由人工填写，系统不代填。空单元格表示未知，不等于 0。"},
        *({"项目": "缺失证据", "内容": text} for text in dossier.missing_evidence),
        *({"项目": f"工作表：{name}", "内容": desc} for name, desc in index),
    ]
    sheets["原始数据"] = tables.get("原始数据", [])
    sheets["表1_清洗Top"] = tables.get("表1", [])
    sheets["表2_商品补齐"] = tables.get("表2", [])
    sheets["表3_分类"] = tables.get("表3", [])

    mm = dossier.market_metrics
    sheets["市场月度"] = _rows(mm.market)
    sheets["细分月度"] = _rows(mm.segments)
    sheets["卖家地区"] = _rows(mm.seller_regions)
    sheets["份额分解"] = _rows(mm.decompositions)
    sheets["跨期比较"] = _rows(mm.period_comparisons)
    if dossier.competition_metrics:
        cm = dossier.competition_metrics
        sheets["集中度"] = _rows(cm.concentration)
        sheets["逐名次销量"] = _rows(cm.rank_points)
        sheets["累计层级"] = _rows(cm.rank_layers)
        sheets["头腰尾"] = _rows(cm.rank_bands)
        sheets["头部虹吸"] = _rows(cm.head_suction)
    if dossier.new_product_analysis:
        npa = dossier.new_product_analysis
        sheets["新品汇总"] = [{"指标": k, "值": v} for k, v in asdict(npa.summary).items()]
        sheets["新品明细"] = _rows(npa.outcomes)
        sheets["新品分组"] = _rows([*npa.account_types, *npa.seller_regions, *npa.segments])
    if dossier.market_analysis:
        ma = dossier.market_analysis
        sheets["研究发现"] = [
            {"类型": "摘要", "标题": "", "内容": ma.summary, "证据ID": ""},
            *({"类型": "发现", "标题": f.title, "内容": f.detail, "证据ID": ", ".join(f.evidence_ids)}
              for f in ma.findings),
            *({"类型": "待研究", "标题": "", "内容": q, "证据ID": ""} for q in ma.follow_up_questions),
        ]
    if dossier.route_plan:
        sheets["后续路线"] = [
            {"路线": s.title, "原因": s.reason, "优先级": s.priority,
             "人工确认": "是" if s.route_id in dossier.route_plan.confirmed_route_ids else "否"}
            for s in dossier.route_plan.suggestions
        ]
    sheets["数据质量"] = [dict(row) for row in dossier.data_quality]
    return {name: rows for name, rows in sheets.items() if rows or name in {"说明", "表1_清洗Top"}}


def write_workbook(
    path: Path, dossier: ResearchDossierInput, tables: dict[str, list[dict[str, Any]]] | None = None
) -> Path:
    return write_xlsx(path, build_workbook_sheets(dossier, tables))
