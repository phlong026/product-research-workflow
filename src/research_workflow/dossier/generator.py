from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from typing import Any, Iterable

from .models import ResearchDossierInput


class ResearchDossierGenerator:
    """Generate a small, portable research dossier.

    The dossier contains research evidence and analysis only. It deliberately has
    no automatic product decision field.
    """

    def generate(self, dossier: ResearchDossierInput, output_dir: str | Path) -> Path:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        csv_dir = root / "csv"
        csv_dir.mkdir(exist_ok=True)

        payload = self._payload(dossier)
        self._write_json(root / "data.json", payload)
        self._write_sources(root / "source_index.json", dossier)
        self._write_csvs(csv_dir, dossier)
        (root / "report.md").write_text(self._markdown(dossier), encoding="utf-8")
        (root / "dashboard.html").write_text(self._html(dossier), encoding="utf-8")
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
            "source_artifacts": [asdict(source) for source in dossier.source_artifacts],
            "data_quality": list(dossier.data_quality),
            "missing_evidence": list(dossier.missing_evidence),
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
        if dossier.competition_metrics:
            self._write_dataclass_csv(
                csv_dir / "concentration.csv", dossier.competition_metrics.concentration
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
            writer.writerows(dict_rows)

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
                lines.append("### BSR 头/腰/尾分层")
                lines.append("")
                lines.extend(["| 分层 | 排名 | 销量 | 份额 | 同比 |", "|---|---|---:|---:|---:|"])
                for band in latest_bands:
                    lines.append(f"| {band.band} | {band.start_rank}-{band.end_rank} | {band.sales:,} | {band.sales_share_pct:.2f}% | {_fmt_pct(band.yoy_pct)} |")
        else:
            lines.append("未提供竞争态势指标。")

        lines.extend(["", "## 5. 新品研究", ""])
        if dossier.new_product_analysis:
            s = dossier.new_product_analysis.summary
            lines.extend(
                [
                    f"- 有效新品：{s.total_products}",
                    f"- 满足项目成功定义：{s.successful_products}",
                    f"- 成功率：{s.success_rate_pct:.2f}%",
                    f"- 当前仍满足存活阈值：{s.surviving_successful_products}",
                    f"- 成功新品中早期达到阈值占比：{s.early_threshold_share_among_successful_pct:.2f}%",
                ]
            )
            if dossier.new_product_analysis.account_types:
                lines.append("")
                lines.append("### 账号类型")
                lines.extend(["| 类型 | 新品数 | 成功数 | 成功率 |", "|---|---:|---:|---:|"])
                for row in dossier.new_product_analysis.account_types:
                    lines.append(f"| {row.account_type} | {row.total_products} | {row.successful_products} | {row.success_rate_pct:.2f}% |")
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
        analysis_html = ""
        if dossier.market_analysis:
            items = "".join(
                f"<li><strong>{escape(f.title)}</strong> — {escape(f.detail)}</li>"
                for f in dossier.market_analysis.findings
            )
            analysis_html = f"<ul>{items}</ul>"
        else:
            analysis_html = "<p>暂无研究发现。</p>"

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
</style>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
</head>
<body><main>
<h1>{title}</h1>
<p class="small">研究材料由系统整理；最终商业判断由人工完成。</p>
<div class="card"><h2>市场月销量</h2><div id="market" class="chart"></div></div>
<div class="card"><h2>细分月销量</h2><div id="segments" class="chart"></div></div>
<div class="card"><h2>研究发现</h2>{analysis_html}</div>
<script>
const payload = {chart_payload};
const market = echarts.init(document.getElementById('market'));
market.setOption({{ tooltip:{{trigger:'axis'}}, xAxis:{{type:'category',data:payload.months}}, yAxis:{{type:'value'}}, series:[{{name:'总销量',type:'line',data:payload.market_sales}}] }});
const segments = echarts.init(document.getElementById('segments'));
segments.setOption({{ tooltip:{{trigger:'axis'}}, legend:{{type:'scroll'}}, xAxis:{{type:'category',data:payload.months}}, yAxis:{{type:'value'}}, series:payload.segment_series }});
window.addEventListener('resize',()=>{{market.resize();segments.resize();}});
</script>
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


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def _fmt_pp(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}pp"
