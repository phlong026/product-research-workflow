"""Display-only report sections shared by report.md and dashboard.html.

Everything here formats numbers already computed by the analysis modules; no
metric is derived in this file. Percent fields are percentage points
(``12.5`` means 12.5%), matching the source models.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable, Sequence

from .models import ResearchDossierInput

SCOPE_TITLE = "口径说明"
SCOPE_NOTES: tuple[str, ...] = (
    "集中度有三种口径,互相不能替代:ASIN 口径(按单个 ASIN 销量排序取 Top N)、"
    "品牌口径(同品牌销量合并后取 Top N)、卖家口径(按 seller_id 合并后取 Top N;"
    "缺 seller_id 的记录不并成同一个卖家,单独列为“未知卖家”)。",
    "单点 vs 累计:单点名次(basis=point)是第 N 名单个 ASIN 的月销量;"
    "累计/区间(basis=cumulative)是 1..N 名或某名次区间的销量之和。两者口径不同,不能直接比较。",
    "新品三个比例分母不同,不得混用:历史成功率(分母=全部有效新品,曾连续达标)、"
    "当期存活率(分母=当期已观测的成功新品)、原文口径留存率(分母=全部有效新品,"
    "分子=连续达标且当期仍达标;当期未观测的新品计入上限、不计入下限)。",
    "预测、外推或样本内统计不等于全市场结论;本资料包的数字仅覆盖所提供的数据样本。",
    "季节性只在 12 个月齐全的年份判断高低月;月份不全的年份只给覆盖提示,不给高低月结论。",
    "缺月不当 0:缺失月份显示为空(“-”),不按 0 计算,涉及缺失月份的同比、环比不计算。",
    "产品迭代进程中,earliest_launch_date 来自上架日期字段;earliest_observed_in_data "
    "只是该分类在所提供数据窗口内首次出现的月份,不是上架日期。",
    "最终商业判断由人工完成;本资料包只提供研究材料。",
)

_DIRECTION = {"up": "上升", "down": "下降", "flat": "持平", "unavailable": "不可得"}


@dataclass(frozen=True, slots=True)
class TableBlock:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str = ""


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def fmt_pp(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}pp"


def fmt_share(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}%"


def fmt_int(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _text(value: object) -> str:
    return "-" if value in (None, "") else str(value)


def markdown_table(block: TableBlock) -> list[str]:
    lines = [f"### {block.title}", ""]
    if block.note:
        lines.extend([block.note, ""])
    lines.append("| " + " | ".join(block.headers) + " |")
    lines.append("|" + "|".join("---" for _ in block.headers) + "|")
    for row in block.rows:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return lines


def html_table(block: TableBlock) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in block.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in block.rows
    )
    note = f'<p class="small">{escape(block.note)}</p>' if block.note else ""
    return (
        f"<h3>{escape(block.title)}</h3>{note}"
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def scope_markdown() -> list[str]:
    return [f"## {SCOPE_TITLE}", "", *[f"- {note}" for note in SCOPE_NOTES], ""]


def scope_html() -> str:
    items = "".join(f"<li>{escape(note)}</li>" for note in SCOPE_NOTES)
    return f"<h2>{SCOPE_TITLE}</h2><ul>{items}</ul>"


# ------------------------------------------------------------------ market


def region_segment_block(dossier: ResearchDossierInput) -> TableBlock | None:
    rows = dossier.market_metrics.region_segments
    if not rows:
        return None
    month = max(row.month for row in rows)
    latest = sorted(
        (row for row in rows if row.month == month),
        key=lambda row: (row.seller_region, -row.sales, row.segment),
    )
    return TableBlock(
        title=f"国别 × 分类透视（{month}）",
        note="占比分母已分别标注:当月合计含未知地区;“已知地区占比”不含未知地区;“地区内占比”为同一地区列内占比。",
        headers=("地区", "分类", "销量", "ASIN数", "占当月销量", "占已知地区销量", "地区内占比", "占当月ASIN"),
        rows=tuple(
            (
                row.seller_region,
                row.segment,
                fmt_int(row.sales),
                str(row.asin_count),
                fmt_share(row.sales_share_of_month_pct),
                fmt_share(row.sales_share_of_known_region_pct),
                fmt_share(row.share_within_region_pct),
                fmt_share(row.asin_share_of_month_pct),
            )
            for row in latest
        ),
    )


def region_change_block(dossier: ResearchDossierInput) -> TableBlock | None:
    rows = dossier.market_metrics.seller_regions
    if not rows:
        return None
    month = max(row.month for row in rows)
    latest = sorted((row for row in rows if row.month == month), key=lambda row: -row.sales)
    return TableBlock(
        title=f"卖家地区环比/同比（{month}）",
        note="基期月份缺失时不给出变化,只显示覆盖提示;原因栏均为待验证假设,不是已验证结论。",
        headers=("地区", "销量环比", "销量同比", "份额环比", "份额同比", "覆盖提示", "待验证假设"),
        rows=tuple(
            (
                row.seller_region,
                fmt_pct(row.sales_mom_pct),
                fmt_pct(row.sales_yoy_pct),
                fmt_pp(row.share_mom_pp),
                fmt_pp(row.share_yoy_pp),
                _text(row.coverage_warning),
                _text(row.cause_hypothesis),
            )
            for row in latest
        ),
    )


def seasonality_block(dossier: ResearchDossierInput) -> TableBlock | None:
    result = dossier.market_metrics.seasonality
    if not result.years:
        return None
    note = "仅 12 个月齐全的年份给出高低月;其余年份只显示覆盖提示。"
    if result.recurring_high_month or result.recurring_low_month:
        note += (
            f" 完整年份中重复出现的高月：{_text(result.recurring_high_month)}；"
            f"重复出现的低月：{_text(result.recurring_low_month)}。"
        )
    if result.coverage_warning:
        note += f" 覆盖提示：{result.coverage_warning}。"
    return TableBlock(
        title="季节性（高低月）",
        note=note,
        headers=("年份", "已观测月数", "12月齐全", "缺失月份", "高月", "高月销量", "低月", "低月销量", "覆盖提示", "待验证假设"),
        rows=tuple(
            (
                str(year.year),
                str(year.months_observed),
                "是" if year.is_complete else "否",
                "、".join(year.missing_months) or "-",
                _text(year.high_month),
                fmt_int(year.high_sales),
                _text(year.low_month),
                fmt_int(year.low_sales),
                _text(year.coverage_warning),
                _text(year.cause_hypothesis),
            )
            for year in result.years
        ),
    )


# ------------------------------------------------------------- competition


def concentration_block(dossier: ResearchDossierInput) -> TableBlock | None:
    comp = dossier.competition_metrics
    if comp is None or not comp.concentration:
        return None
    return TableBlock(
        title="集中度三种口径并列",
        note="ASIN 口径、品牌口径、卖家口径(seller_id)分别计算,不可互相替代;未知卖家 = 缺 seller_id 的记录,单独统计。",
        headers=(
            "月份",
            "Top3 ASIN口径", "Top10 ASIN口径",
            "Top3 品牌口径", "Top10 品牌口径",
            "Top3 卖家口径", "Top10 卖家口径",
            "未知卖家销量占比", "未知卖家ASIN数",
        ),
        rows=tuple(
            (
                row.month,
                fmt_share(row.top3_asin_share_pct), fmt_share(row.top10_asin_share_pct),
                fmt_share(row.top3_brand_share_pct), fmt_share(row.top10_brand_share_pct),
                fmt_share(row.top3_seller_share_pct), fmt_share(row.top10_seller_share_pct),
                fmt_share(row.unknown_seller_share_pct), str(row.unknown_seller_asin_count),
            )
            for row in sorted(comp.concentration, key=lambda r: r.month)
        ),
    )


def point_rank_matrix_block(dossier: ResearchDossierInput) -> TableBlock | None:
    comp = dossier.competition_metrics
    if comp is None or not comp.point_rank_matrix.rows:
        return None
    matrix = comp.point_rank_matrix
    return TableBlock(
        title="单点名次逐月销量矩阵（basis=point）",
        note="每格为该名次单个 ASIN 的月销量,不是累计值;ASIN 数不足该名次时留空(“-”),不补 0。",
        headers=("月份", "排序口径", *[f"{rank}名" for rank in matrix.ranks]),
        rows=tuple(
            (row.month, row.ordering_basis, *[fmt_int(row.point_sales.get(str(rank))) for rank in matrix.ranks])
            for row in sorted(matrix.rows, key=lambda r: r.month)
        ),
    )


def point_rank_change_block(dossier: ResearchDossierInput) -> TableBlock | None:
    comp = dossier.competition_metrics
    if comp is None or not comp.point_rank_changes:
        return None
    month = max(row.current_month for row in comp.point_rank_changes)
    latest = [row for row in comp.point_rank_changes if row.current_month == month]
    return TableBlock(
        title=f"单点名次变化（basis=point，当前月 {month}）",
        note="仅陈述两期同一名次的销量差异,不含判断。",
        headers=("名次", "基准月", "当前月", "基准销量", "当前销量", "绝对变化", "相对变化", "方向"),
        rows=tuple(
            (
                str(row.rank),
                row.base_month,
                row.current_month,
                fmt_int(row.base_point_sales),
                fmt_int(row.current_point_sales),
                "-" if row.abs_change is None else f"{row.abs_change:+,}",
                fmt_pct(row.rel_change_pct),
                _DIRECTION.get(row.direction, row.direction),
            )
            for row in latest
        ),
    )


# ------------------------------------------------------------- new products


def retention_rows(dossier: ResearchDossierInput) -> list[tuple[str, float | int | str | None, str, str]]:
    """(指标, 值, 值类型 pct|int|text, 口径说明) rows shared by report and workbook."""
    analysis = dossier.new_product_analysis
    if analysis is None:
        return []
    s = analysis.summary
    consecutive = analysis.config.consecutive_months
    return [
        ("历史成功率", s.success_rate_pct, "pct",
         f"分母=全部有效新品({s.total_products});分子=曾连续{consecutive}个月达标({s.successful_products})"),
        ("当期存活率", s.survival_rate_among_successful_pct, "pct",
         f"分母=当期已观测的成功新品({s.observed_successful_products});当期未观测 {s.unknown_successful_products} 个不计入"),
        ("原文口径留存率(下限)", s.retention_rate_pct, "pct",
         f"分母=全部有效新品({s.retention_denominator_products});分子=连续达标且当期仍达标({s.retained_products});未知不计入分子"),
        ("原文口径留存率(上限)", s.retention_rate_upper_bound_pct, "pct",
         f"同分母;未知 {s.retention_unknown_products} 个按留存计入分子"),
        ("留存率口径说明", s.retention_denominator_note, "text", "来自新品分析模块"),
        ("新品成功阈值(月销量)", s.promotion_waist_sales_threshold, "int", "判定历史成功的月销量阈值"),
        ("留存阈值(月销量)", s.retention_sales_threshold, "int", "判定留存/当期存活的月销量阈值"),
        ("当前月", s.current_month, "text", "留存与存活的观察月"),
        ("当期观测覆盖率(成功新品)", s.current_observation_coverage_pct, "pct", "已观测成功新品 / 成功新品"),
        ("早期窗口完整新品数", s.early_window_complete_products, "int", "早期窗口月份全部被观测到的有效新品"),
        ("早期窗口不完整新品数", s.early_window_incomplete_products, "int", "早期窗口含缺月,不进入早期达标比例分母"),
        ("早期窗口达标新品数", s.early_window_reached_products, "int", "早期窗口完整且达标"),
        ("早期窗口达标比例", s.early_window_reached_share_pct, "pct", "分母=早期窗口完整的有效新品"),
    ]


def retention_block(dossier: ResearchDossierInput) -> TableBlock | None:
    rows = retention_rows(dossier)
    if not rows:
        return None

    def render(value: object, kind: str) -> str:
        if kind == "pct":
            return fmt_share(value)  # type: ignore[arg-type]
        return _text(value)

    return TableBlock(
        title="历史成功率 / 当期存活率 / 原文口径留存率（三者口径不同，不得混用）",
        headers=("指标", "值", "分母与口径"),
        rows=tuple((label, render(value, kind), note) for label, value, kind, note in rows),
    )


# ---------------------------------------------------------------- iteration


def iteration_block(dossier: ResearchDossierInput) -> TableBlock | None:
    if dossier.iteration is None:
        return None
    return TableBlock(
        title="产品迭代进程",
        note="；".join(dossier.iteration.notes),
        headers=(
            "顺序", "分类",
            "earliest_launch_date（上架日期）", "最早上架ASIN",
            "earliest_observed_in_data（数据首次出现月，非上架日期）",
            "ASIN数", "有效上架日期ASIN数", "排除:缺失", "排除:无法解析", "排除:未来日期", "排除:过早日期",
        ),
        rows=tuple(
            (
                _text(seg.order), seg.segment, _text(seg.earliest_launch_date), _text(seg.earliest_launch_asin),
                seg.earliest_observed_in_data, str(seg.asin_count), str(seg.valid_launch_date_asin_count),
                str(seg.excluded_missing), str(seg.excluded_unparseable),
                str(seg.excluded_future), str(seg.excluded_too_early),
            )
            for seg in dossier.iteration.segments
        ),
    )


def present(blocks: Iterable[TableBlock | None]) -> list[TableBlock]:
    return [block for block in blocks if block is not None]


def blocks_for_dashboard(dossier: ResearchDossierInput) -> Sequence[TableBlock]:
    return present(
        [
            region_segment_block(dossier),
            region_change_block(dossier),
            seasonality_block(dossier),
            concentration_block(dossier),
            point_rank_matrix_block(dossier),
            point_rank_change_block(dossier),
            retention_block(dossier),
            iteration_block(dossier),
        ]
    )
