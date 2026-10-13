from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass, replace
from hashlib import sha256
from html import escape
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import ResearchDossierInput
from . import external as external_section
from . import sections
from .workbook import WORKBOOK_FILENAME, WORKBOOK_SKIPPED_NOTE, openpyxl_available, write_research_workbook


class ResearchDossierGenerator:
    """Generate a small, portable research dossier.

    The dossier contains research evidence and analysis only. It deliberately has
    no automatic product decision field.
    """

    def generate(
        self,
        dossier: ResearchDossierInput,
        output_dir: str | Path,
        *,
        evidence_files: dict[str, Path] | None = None,
        approval_history: dict | None = None,
        include_workbook: bool = True,
        generated_at: str | None = None,
    ) -> Path:
        """Write the dossier bundle.

        With ``include_workbook`` the comprehensive Excel workbook is added (and
        hashed into the manifest). If openpyxl is missing it is skipped and the
        gap is recorded in ``missing_evidence`` instead of failing the run.
        """
        write_workbook = include_workbook and openpyxl_available()
        if include_workbook and not write_workbook:
            dossier = replace(
                dossier,
                missing_evidence=[*dossier.missing_evidence, WORKBOOK_SKIPPED_NOTE],
            )
        root = Path(output_dir)
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise ValueError("导出目录必须为空或不存在;请使用新的版本目录保留旧研究资料包")
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".dossier-", dir=root.parent))
        try:
            self._generate_into(dossier, staging, write_workbook=write_workbook, generated_at=generated_at)
            if evidence_files:
                evidence_dir = staging / "evidence"
                evidence_dir.mkdir()
                for name, source_path in evidence_files.items():
                    if Path(name).name != name:
                        raise ValueError("Evidence filenames must not contain paths")
                    shutil.copyfile(source_path, evidence_dir / name)
                self._write_json(evidence_dir / "approvals.json", approval_history or {})
                self._write_manifest(staging)
            if root.exists():
                root.rmdir()  # Fails safely if another writer populated it.
            staging.replace(root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return root

    def _generate_into(self, dossier: ResearchDossierInput, root: Path, *,
                       write_workbook: bool = False, generated_at: str | None = None) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        csv_dir = root / "csv"
        csv_dir.mkdir(exist_ok=True)

        payload = self._payload(dossier)
        self._write_json(root / "data.json", payload)
        self._write_sources(root / "source_index.json", dossier)
        self._write_csvs(csv_dir, dossier)
        (root / "report.md").write_text(self._markdown(dossier), encoding="utf-8")
        (root / "dashboard.html").write_text(self._html(dossier), encoding="utf-8")
        if write_workbook:
            write_research_workbook(dossier, root / WORKBOOK_FILENAME, generated_at=generated_at)
        self._write_manifest(root)
        return root

    def _payload(self, dossier: ResearchDossierInput) -> dict[str, Any]:
        return {
            "project": {
                "project_id": dossier.project_id,
                "product": dossier.product,
                "marketplace": dossier.marketplace,
                "decision_owner": "human",
                "metadata": dict(dossier.metadata),
            },
            "market_metrics": dossier.market_metrics.to_dict(),
            "market_analysis": (
                dossier.market_analysis.to_dict() if dossier.market_analysis else None
            ),
            "competition_metrics": (
                dossier.competition_metrics.to_dict()
                if dossier.competition_metrics
                else None
            ),
            "new_product_analysis": (
                dossier.new_product_analysis.to_dict()
                if dossier.new_product_analysis
                else None
            ),
            "research_route_plan": dossier.route_plan.to_dict() if dossier.route_plan else None,
            "iteration": dossier.iteration.to_dict() if dossier.iteration else None,
            "source_artifacts": [asdict(source) for source in dossier.source_artifacts],
            "data_quality": list(dossier.data_quality),
            "missing_evidence": list(dossier.missing_evidence),
            **({"external": dossier.external} if dossier.external is not None else {}),
        }

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _write_sources(self, path: Path, dossier: ResearchDossierInput) -> None:
        self._write_json(
            path,
            {
                "project_id": dossier.project_id,
                "sources": [asdict(source) for source in dossier.source_artifacts],
            },
        )

    def _write_csvs(self, csv_dir: Path, dossier: ResearchDossierInput) -> None:
        self._write_dataclass_csv(csv_dir / "market.csv", dossier.market_metrics.market)
        self._write_dataclass_csv(csv_dir / "segments.csv", dossier.market_metrics.segments)
        self._write_dataclass_csv(
            csv_dir / "seller_regions.csv", dossier.market_metrics.seller_regions
        )
        self._write_dataclass_csv(
            csv_dir / "market_decomposition.csv", dossier.market_metrics.decompositions
        )
        self._write_dataclass_csv(
            csv_dir / "period_comparisons.csv", dossier.market_metrics.period_comparisons or []
        )
        self._write_dataclass_csv(
            csv_dir / "market_region_segments.csv", dossier.market_metrics.region_segments
        )
        self._write_dataclass_csv(
            csv_dir / "market_seasonality.csv",
            [
                {**asdict(year), "missing_months": "、".join(year.missing_months)}
                for year in dossier.market_metrics.seasonality.years
            ],
        )
        if dossier.competition_metrics:
            self._write_dataclass_csv(
                csv_dir / "concentration.csv", dossier.competition_metrics.concentration
            )
            matrix = dossier.competition_metrics.point_rank_matrix
            self._write_dataclass_csv(
                csv_dir / "point_rank_matrix.csv",
                [
                    {
                        "month": row.month,
                        "ordering_basis": row.ordering_basis,
                        "basis": row.basis,
                        **{f"rank_{rank}": row.point_sales.get(str(rank)) for rank in matrix.ranks},
                    }
                    for row in matrix.rows
                ],
            )
            self._write_dataclass_csv(
                csv_dir / "point_rank_changes.csv", dossier.competition_metrics.point_rank_changes
            )
            self._write_dataclass_csv(
                csv_dir / "rank_layers.csv", dossier.competition_metrics.rank_layers
            )
            self._write_dataclass_csv(
                csv_dir / "rank_bands.csv", dossier.competition_metrics.rank_bands
            )
            self._write_dataclass_csv(
                csv_dir / "head_suction.csv", dossier.competition_metrics.head_suction
            )
        if dossier.new_product_analysis:
            self._write_dataclass_csv(
                csv_dir / "new_product_summary.csv", [asdict(dossier.new_product_analysis.summary)]
            )
            self._write_dataclass_csv(
                csv_dir / "new_product_outcomes.csv", dossier.new_product_analysis.outcomes
            )
            self._write_dataclass_csv(
                csv_dir / "new_product_account_types.csv",
                dossier.new_product_analysis.account_types,
            )
            self._write_dataclass_csv(
                csv_dir / "new_product_seller_regions.csv",
                dossier.new_product_analysis.seller_regions,
            )
            self._write_dataclass_csv(
                csv_dir / "new_product_segments.csv",
                dossier.new_product_analysis.segments,
            )
        if dossier.iteration:
            self._write_dataclass_csv(csv_dir / "product_iteration.csv", dossier.iteration.segments)

    @staticmethod
    def _write_dataclass_csv(path: Path, rows: Iterable[Any]) -> None:
        values = list(rows)
        if not values:
            path.write_text("", encoding="utf-8")
            return
        dict_rows = [asdict(row) if is_dataclass(row) else dict(row) for row in values]
        fields: list[str] = []
        for row in dict_rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: _csv_cell(value) for key, value in row.items()} for row in dict_rows)

    def _markdown(self, dossier: ResearchDossierInput) -> str:
        market = sorted(dossier.market_metrics.market, key=lambda row: row.month)
        latest = market[-1] if market else None
        lines = [
            f"# {dossier.product}｜Research Dossier",
            "",
            f"- Project ID: `{dossier.project_id}`",
            f"- Marketplace: `{dossier.marketplace}`",
            "- 最终商业判断：由人工完成；本报告仅提供研究材料。",
            "",
            *sections.scope_markdown(),
            "## 0. 数据完整度与待补资料",
            "",
        ]
        if dossier.data_quality:
            for item in dossier.data_quality:
                label = item.get("label", "数据质量")
                value = item.get("value", "-")
                lines.append(f"- {label}：{value}")
        else:
            lines.append("- 未提供额外数据质量摘要。")
        if dossier.missing_evidence:
            lines.append("- 待补资料：" + "；".join(dossier.missing_evidence))
        else:
            lines.append("- 待补资料：无已登记缺口。")
        lines.extend(["", "## 1. 市场概览", ""])
        if latest:
            lines.extend(
                [
                    f"- 最新月份：{latest.month}",
                    f"- Top样本月销量：{latest.total_sales:,}",
                    f"- ASIN 数：{latest.asin_count}",
                    f"- 同比：{_fmt_pct(latest.yoy_pct)}",
                    f"- 环比：{_fmt_pct(latest.mom_pct)}",
                ]
            )
        else:
            lines.append("- 暂无市场指标。")

        if dossier.market_metrics.period_comparisons:
            lines.extend(["", "### 跨期窗口比较", "", "| 范围 | 对象 | 基准期 | 当前期 | 变化 | 份额变化 |", "|---|---|---|---|---:|---:|"])
            for row in dossier.market_metrics.period_comparisons:
                if row.missing_base_months or row.missing_current_months:
                    lines.append(f"缺月，暂停比较：基期 {list(row.missing_base_months)}；当前期 {list(row.missing_current_months)}。")
                lines.append(
                    f"| {row.scope} | {row.segment or '-'} | {row.base_period} | {row.current_period} | "
                    f"{_fmt_pct(row.change_pct)} | {_fmt_pp(row.share_change_pp)} |"
                )

        lines.extend(["", "## 2. 产品形态 / 细分结构", ""])
        if latest:
            latest_segments = [
                row for row in dossier.market_metrics.segments if row.month == latest.month
            ]
            latest_segments.sort(key=lambda row: row.sales, reverse=True)
            lines.extend(
                [
                    "| 细分 | 销量 | 份额 | ASIN数 | 同比 |",
                    "|---|---:|---:|---:|---:|",
                ]
            )
            for row in latest_segments:
                lines.append(
                    f"| {row.segment} | {row.sales:,} | {row.sales_share_pct:.2f}% | "
                    f"{row.asin_count} | {_fmt_pct(row.yoy_pct)} |"
                )
        else:
            lines.append("暂无细分数据。")

        lines.extend(["", "### 卖家地区结构", ""])
        if latest:
            latest_regions = [row for row in dossier.market_metrics.seller_regions if row.month == latest.month]
            latest_regions.sort(key=lambda row: row.sales, reverse=True)
            lines.extend(["| 地区 | 销量 | 销量份额 | ASIN数 |", "|---|---:|---:|---:|"])
            for row in latest_regions:
                lines.append(f"| {row.seller_region} | {row.sales:,} | {row.sales_share_pct:.2f}% | {row.asin_count} |")
        else:
            lines.append("暂无卖家地区数据。")

        _extend_blocks(
            lines,
            [
                sections.region_segment_block(dossier),
                sections.region_change_block(dossier),
                sections.seasonality_block(dossier),
                sections.iteration_block(dossier)
                or sections.TableBlock("产品迭代进程", (), (), "本次运行未提供。"),
            ],
        )

        lines.extend(["", "## 3. 市场研究发现", ""])
        if dossier.market_analysis:
            lines.append(dossier.market_analysis.summary or "已完成研究分析。")
            lines.append("")
            for finding in dossier.market_analysis.findings:
                refs = ", ".join(finding.evidence_ids)
                lines.extend(
                    [
                        f"### {finding.title}",
                        "",
                        finding.detail,
                        "",
                        f"证据：`{refs}`",
                        "",
                    ]
                )
            if dossier.market_analysis.follow_up_questions:
                lines.append("### 待进一步研究")
                lines.append("")
                lines.extend(f"- {q}" for q in dossier.market_analysis.follow_up_questions)
        else:
            lines.append("未提供 Market Research Agent 输出。")

        lines.extend(["", "## 4. 竞争态势", ""])
        if dossier.competition_metrics and dossier.competition_metrics.concentration:
            latest_c = sorted(
                dossier.competition_metrics.concentration, key=lambda row: row.month
            )[-1]
            lines.extend(
                [
                    f"- {latest_c.month} Top3 ASIN 销量占比：{latest_c.top3_asin_share_pct:.2f}%",
                    f"- {latest_c.month} Top10 ASIN 销量占比：{latest_c.top10_asin_share_pct:.2f}%",
                    f"- {latest_c.month} Top3 品牌销量占比：{latest_c.top3_brand_share_pct:.2f}%",
                    f"- {latest_c.month} Top10 品牌销量占比：{latest_c.top10_brand_share_pct:.2f}%",
                ]
            )
            latest_bands = [x for x in dossier.competition_metrics.rank_bands if x.month == latest_c.month]
            if latest_bands:
                lines.append("")
                lines.append("### BSR 头/腰/尾分层（basis=cumulative，区间累计，非单点名次）")
                lines.append("")
                lines.extend(["| 分层 | 排名 | 销量 | 份额 | 同比 |", "|---|---|---:|---:|---:|"])
                for band in latest_bands:
                    lines.append(f"| {band.band} | {band.start_rank}-{band.end_rank} | {band.sales:,} | {band.sales_share_pct:.2f}% | {_fmt_pct(band.yoy_pct)} |")
        else:
            lines.append("未提供竞争态势指标。")
        _extend_blocks(
            lines,
            [
                sections.concentration_block(dossier),
                sections.point_rank_matrix_block(dossier),
                sections.point_rank_change_block(dossier),
            ],
        )

        lines.extend(["", "## 5. 新品研究", ""])
        if dossier.new_product_analysis:
            s = dossier.new_product_analysis.summary
            lines.extend(
                [
                    f"- 有效新品：{s.total_products}",
                    f"- 满足项目成功定义：{s.successful_products}",
                    f"- 成功率：{s.success_rate_pct:.2f}%",
                    f"- 当前仍满足存活阈值：{s.surviving_successful_products}",
                    f"- 已成功新品当期观测覆盖率：{_fmt_pct(s.current_observation_coverage_pct)}；未观测 {s.unknown_successful_products} 个",
                    f"- 已观测成功新品存活率：{_fmt_pct(s.survival_rate_among_successful_pct)}（分母仅含已观测成功新品）",
                    f"- 成功新品中早期达到阈值占比：{s.early_threshold_share_among_successful_pct:.2f}%",
                ]
            )
            if dossier.new_product_analysis.account_types:
                lines.append("")
                lines.append("### 账号类型")
                lines.extend(["| 类型 | 新品数 | 成功数 | 成功率 |", "|---|---:|---:|---:|"])
                for row in dossier.new_product_analysis.account_types:
                    lines.append(f"| {row.account_type} | {row.total_products} | {row.successful_products} | {row.success_rate_pct:.2f}% |")
            _extend_blocks(lines, [sections.retention_block(dossier)])
        else:
            lines.append("未提供新品研究结果。")
        lines.extend(["", "## 6. 后续研究路线", ""])
        if dossier.route_plan:
            for item in dossier.route_plan.suggestions:
                status = "已人工确认" if item.route_id in dossier.route_plan.confirmed_route_ids else "待确认"
                lines.append(f"- **{item.title}**（{status}）：{item.reason}")
        else:
            lines.append("未生成后续研究路线。")

        lines.extend(["", "## 7. 原始资料索引", ""])
        if dossier.source_artifacts:
            lines.extend(
                [
                    "| ID | 文件/来源 | 类型 | SHA256 | 说明 |",
                    "|---|---|---|---|---|",
                ]
            )
            for source in dossier.source_artifacts:
                lines.append(
                    f"| {source.artifact_id} | {source.name} | {source.kind} | "
                    f"{source.sha256 or '-'} | {source.note or '-'} |"
                )
        else:
            lines.append("暂无原始资料索引。")

        if dossier.external is not None:
            lines.extend(external_section.external_markdown(dossier.external))

        lines.extend([
            "",
            "## 8. 人工最终判断（系统不自动填写）",
            "",
            "- 最终结论：□ YES  □ NO  □ 继续研究",
            "- 决策人：",
            "- 决策日期：",
            "- 决策原因：",
        ])

        return "\n".join(lines).rstrip() + "\n"

    def _html(self, dossier: ResearchDossierInput) -> str:
        market = sorted(dossier.market_metrics.market, key=lambda row: row.month)
        months = [row.month for row in market]
        sales = [row.total_sales for row in market]
        segment_names = sorted({row.segment for row in dossier.market_metrics.segments})
        segment_series = []
        for segment in segment_names:
            by_month = {
                row.month: row.sales
                for row in dossier.market_metrics.segments
                if row.segment == segment
            }
            segment_series.append(
                {"name": segment, "type": "line", "data": [by_month.get(m, 0) for m in months]}
            )

        chart_payload = json.dumps(
            {"months": months, "market_sales": sales, "segment_series": segment_series},
            ensure_ascii=False,
        ).replace("</", "<\\/")
        title = escape(f"{dossier.product} Research Dashboard")
        market_svg = _svg_chart(months, [("总销量", sales)])
        segments_svg = _svg_chart(months, [(s["name"], s["data"]) for s in segment_series])
        analysis_html = ""
        if dossier.market_analysis:
            items = "".join(
                f"<li><strong>{escape(f.title)}</strong> — {escape(f.detail)}</li>"
                for f in dossier.market_analysis.findings
            )
            analysis_html = f"<ul>{items}</ul>"
        else:
            analysis_html = "<p>暂无研究发现。</p>"

        extra_cards = "".join(
            f'<div class="card">{sections.html_table(block)}</div>'
            for block in sections.blocks_for_dashboard(dossier)
        )
        if dossier.external is not None:
            extra_cards += (
                f'<div class="card"><h2>{escape(external_section.TITLE)}</h2>'
                + "".join(sections.html_table(b) for b in external_section.external_dashboard_blocks(dossier.external))
                + "</div>"
            )

        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin: 0; background:#f6f7f9; color:#171a1f; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 56px; }}
.card {{ background:white; border:1px solid #e5e7eb; border-radius:12px; padding:20px; margin:16px 0; }}
.chart {{ width:100%; height:420px; }}
.small {{ color:#667085; font-size:14px; }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; font-size:13px; }}
th, td {{ border:1px solid #e5e7eb; padding:4px 8px; text-align:right; white-space:nowrap; }}
th:first-child, td:first-child {{ text-align:left; }}
</style>

</head>
<body><main>
<h1>{title}</h1>
<p class="small">研究材料由系统整理；最终商业判断由人工完成。</p>
<div class="card"><h2>市场月销量</h2>{market_svg}</div>
<div class="card"><h2>细分月销量</h2>{segments_svg}</div>
<div class="card"><h2>研究发现</h2>{analysis_html}</div>
<div class="card">{sections.scope_html()}</div>
{extra_cards}
<script id="chart-data" type="application/json">{chart_payload}</script>
</main></body></html>
"""

    def _write_manifest(self, root: Path) -> None:
        entries = []
        for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"):
            raw = path.read_bytes()
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": len(raw),
                    "sha256": sha256(raw).hexdigest(),
                }
            )
        self._write_json(root / "manifest.json", {"files": entries})


def _extend_blocks(lines: list[str], blocks: Iterable[sections.TableBlock | None]) -> None:
    for block in blocks:
        if block is None:
            continue
        lines.append("")
        if block.headers:
            lines.extend(sections.markdown_table(block))
        else:  # 数据缺失:只写标题与说明
            lines.extend([f"### {block.title}", "", block.note])


_fmt_pct = sections.fmt_pct
_fmt_pp = sections.fmt_pp


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _svg_chart(months: list[str], series: list[tuple[str, list[int]]]) -> str:
    """Self-contained chart; no CDN, JavaScript or network needed to view evidence."""
    if not months:
        return "<p>暂无数据。</p>"
    maximum = max((v for _, values in series for v in values), default=0) or 1
    width, height = 960, 320
    colors = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#db2777"]
    parts = ['<svg viewBox="0 0 960 360" role="img" aria-label="月销量折线图" style="width:100%;height:auto">']
    for i in range(5):
        y = 20 + i * 65
        parts.append(f'<line x1="80" y1="{y}" x2="940" y2="{y}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="70" y="{y+4}" text-anchor="end" font-size="12">{maximum*(4-i)/4:,.0f}</text>')
    for i, month in enumerate(months):
        x = 80 + i * 860 / max(1, len(months)-1)
        parts.append(f'<text x="{x}" y="310" text-anchor="middle" font-size="12">{escape(month)}</text>')
    for index, (name, values) in enumerate(series):
        color = colors[index % len(colors)]
        points = [(80+i*860/max(1,len(months)-1), 280-value/maximum*260) for i,value in enumerate(values)]
        points_text = " ".join(f"{x:.2f},{y:.2f}" for x,y in points)
        parts.append(f'<polyline points="{points_text}" fill="none" stroke="{color}" stroke-width="2"/>')
        for (x,y), month, value in zip(points, months, values):
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"><title>{escape(name)} {escape(month)}: {value:,}</title></circle>')
    parts.append('</svg>')
    parts.append('<p>' + ' · '.join(f'<span style="color:{colors[i % len(colors)]}">{escape(name)}</span>' for i,(name,_) in enumerate(series)) + '</p>')
    return "".join(parts)
