"""外部证据与预测章节(纯展示:只格式化已登记/已确定性计算的数据,不新算指标、不下结论)。

数据来自 ResearchDossierInput.external(由 CLI 校验哈希后装载)。同一份 ExtTable 同时渲染
report.md、dashboard.html 与 Excel 工作表;数值在 Excel 中保持数值类型。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from research_workflow.competition_external.ledger import assert_no_decision_language
from research_workflow.competition_external.patents import PATENT_DISCLAIMER

from . import sections

TITLE = "外部证据与预测"
NOT_PROVIDED = "本次运行未提供"
FORECAST_TAG = "预测·假设见下"
OBSERVED_TAG = "观测/声明值"
CONVERTED_TAG = "按覆盖率换算·假设见下"
INTRO_NOTES = (
    "本节数字均来自用户导入的外部文件或对其做的确定性外推;每条数字标注来源、日期与证据 ID。",
    f"凡预测值一律标注「{FORECAST_TAG}」;预测只是外推,不是观测,不含饱和、价格与宏观因素。",
    "缺失与来源不可访问的证据在下方醒目列出;缺失不当 0,「未检索」不等于「未发现」。",
    "本节只陈述事实与口径,不含商业结论;最终商业判断由人工完成。",
)
KIND_LABELS = {
    "trends": "Google Trends 导出", "report-excerpts": "研报摘录", "tiktok-units": "TikTok 渠道台数",
    "patents": "专利记录", "patent-search-log": "专利检索日志", "ads": "广告/流量/评价/CPC 证据",
    "big-brand": "大品牌核查", "offsite": "站外证据", "cross-category": "跨类目 ASIN",
    "keywords": "关键词搜索量", "scenarios": "使用场景", "voc": "用户评论(VOC)主题",
}
KINDS = tuple(KIND_LABELS)
_SCOPE_LABELS = {"top_sample": "Top 样本", "amazon_channel": "Amazon 渠道", "full_market": "全市场"}
_SCENARIOS = {"low": "低", "mid": "中", "high": "高"}
_COHORTS = {"head_waist_tail": "头腰尾", "new_product": "新品"}
_BRAND_STATUS = {
    "candidate_big_brand": "候选大品牌(仅按 ABA 阈值规则)", "not_candidate": "未达阈值",
    "unverified": "未核查(ABA 排名缺失)",
}
_ASSUMPTION_ZH = {
    "extrapolation only; no saturation, price, seasonality or macro effects": "仅为外推,不含饱和、价格、季节性与宏观因素",
    "no complete low/high scenarios supplied: interval not produced": "未同时提供低/高情景,未生成区间",
    "no complete low/high scenario supplied: interval not produced": "未同时提供低/高情景,未生成区间",
    "constant annual growth, no saturation, price or seasonality effects": "假设年增速恒定,不含饱和、价格与季节性影响",
}

# 系统自己写的固定文案必须过库里的禁词守卫(用户数据不在此列)
assert_no_decision_language([
    TITLE, FORECAST_TAG, OBSERVED_TAG, CONVERTED_TAG, *INTRO_NOTES, *KIND_LABELS.values(),
    *_SCOPE_LABELS.values(), *_COHORTS.values(), *_BRAND_STATUS.values(), *_ASSUMPTION_ZH.values(),
])


@dataclass(frozen=True, slots=True)
class ExtTable:
    sheet: str
    title: str
    headers: tuple[str, ...]
    kinds: tuple[str, ...]  # text | int | num | pct(百分点,份额) | chg(百分点,带符号变化)
    rows: tuple[tuple[object, ...], ...] | None  # None = 本次运行未提供
    md_rows: tuple[tuple[object, ...], ...] | None = None  # 报告里的精简行;None 表示与 rows 相同
    notes: tuple[str, ...] = ()

    def to_block(self, *, inline_notes: bool = True) -> sections.TableBlock:
        if self.rows is None:
            return sections.TableBlock(self.title, (), (), NOT_PROVIDED + "。")
        rows = self.rows if self.md_rows is None else self.md_rows
        extra = []
        if self.md_rows is not None:
            extra.append("报告内为精简行,完整序列见 Excel 工作簿与 evidence/")
        if not rows:
            extra.append("已导入,但无记录行")
        note = ";".join([*(self.notes if inline_notes else ()), *extra])
        return sections.TableBlock(
            self.title, self.headers,
            tuple(tuple(_fmt(value, kind) for value, kind in zip(row, self.kinds)) for row in rows),
            note,
        )


def _fmt(value: object, kind: str) -> str:
    if value is None or value == "":
        return "-"
    if kind == "int":
        return f"{int(value):,}"  # type: ignore[call-overload]
    if kind == "num":
        return f"{float(value):,.2f}"  # type: ignore[arg-type]
    if kind == "pct":
        return sections.fmt_share(float(value))  # type: ignore[arg-type]
    if kind == "chg":
        return sections.fmt_pct(float(value))  # type: ignore[arg-type]
    return str(value).replace("\n", " ")


def _entries(ext: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return list(ext.get(kind) or [])


def _times100(value: float | None) -> float | None:
    return None if value is None else value * 100


def _tag(is_forecast: bool) -> str:
    return FORECAST_TAG if is_forecast else OBSERVED_TAG


def _assumption(text: str) -> str:
    if text in _ASSUMPTION_ZH:
        return _ASSUMPTION_ZH[text]
    if text.startswith("evidence insufficient: no conclusion produced;"):
        return "证据不足,未产出结论:" + text.split(";", 1)[1].strip()
    try:
        assert_no_decision_language([text])
    except ValueError:
        return "(英文假设原文含受限词,详见 evidence/ 中的 JSON)"
    return text


def _source_lookup(ext: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """证据 ID -> (来源名, 日期),覆盖研报摘录、TikTok 台数与市场预测。"""
    table: dict[str, tuple[str, str]] = {}
    for entry in _entries(ext, "report-excerpts"):
        for item in entry["data"]:
            table[item["claim_id"]] = (item["source_name"], item["as_of"])
    for evidence_id, src in (ext.get("tiktok_sources") or {}).items():
        table.setdefault(evidence_id, (src["source_name"], src["as_of"]))
    forecast = ext.get("forecast")
    if forecast:
        ev = forecast["evidence"]
        table.setdefault(ev["id"], (ev["source_name"], ev["as_of"]))
    return table


def _sources_for(lookup: dict[str, tuple[str, str]], ids: Iterable[str]) -> tuple[str, str]:
    found = [lookup[i] for i in ids if i in lookup]
    names = "、".join(dict.fromkeys(name for name, _ in found)) or "-"
    dates = "、".join(dict.fromkeys(day for _, day in found if day)) or "-"
    return names, dates


def _evidence_locator(evidence: dict[str, Any]) -> str:
    return evidence.get("url") or Path(evidence.get("file_path") or "-").name


# ------------------------------------------------------------------ 缺失清单


def external_gaps(ext: dict[str, Any] | None) -> list[str]:
    """未导入类别逐项「尚未提供」+ 已登记但缺失/不可访问/未检索的证据。"""
    ext = ext or {}
    gaps = [f"外部证据尚未提供:{label}" for kind, label in KIND_LABELS.items() if not _entries(ext, kind)]
    if not ext.get("forecast"):
        gaps.append("外部证据尚未提供:市场规模预测(未运行 build-market-forecast)")
    if not ext.get("cross_channel"):
        gaps.append("外部证据尚未提供:跨渠道事实整合(未运行 build-cross-channel)")
    for entry in _entries(ext, "report-excerpts"):
        gaps += [f"研报摘录 {i['claim_id']} 来源不可访问:{i['source_name'] or '-'}"
                 for i in entry["data"] if i["status"] != "present"]
    for entry in _entries(ext, "patent-search-log"):
        gaps += [f"专利检索未执行:{log['query']}({log['database']})" for log in entry["data"] if log["status"] == "not_searched"]
    for entry in _entries(ext, "offsite"):
        for ledger in entry["data"]:
            gaps += [f"站外检索未执行:{s['brand']} / {s['query']}({s['engine_tool']})"
                     for s in ledger["searches"] if s["status"] == "not_searched"]
    for entry in _entries(ext, "big-brand"):
        if entry["data"]["status"] == "unverified":
            gaps.append(f"大品牌核查未完成(ABA 排名缺失):{entry['data']['brand']}")
    for analysis in ext.get("tiktok_analysis") or []:
        label = f"{analysis['geo']}/{analysis['segment']}"
        if analysis["missing_months_2026"]:
            missing = analysis["missing_months_2026"]
            gaps.append(f"TikTok {label} 2026 逐月数据未提供" if len(missing) == 12
                        else f"TikTok {label} 缺失 2026 月份:" + "、".join(missing))
        if analysis["cagr_2024_2026"]["status"] != "ok":
            gaps.append(f"TikTok {label} 2024-2026 CAGR 证据不足:{analysis['cagr_2024_2026']['missing_reason']}")
    return gaps


# ------------------------------------------------------------------ 各表


def _trends(ext: dict[str, Any]) -> ExtTable:
    entries = _entries(ext, "trends")
    headers = ("关键词", "月份", "Trends 指数(0-100)", "同比", "地域", "时间范围", "来源", "取数日期", "来源链接", "证据ID(指数)", "证据ID(同比)")
    kinds = ("text", "text", "num", "chg", "text", "text", "text", "text", "text", "text", "text")
    if not entries:
        return ExtTable("外部_趋势", "Google Trends", headers, kinds, None)
    rows: list[tuple[object, ...]] = []
    short: list[tuple[object, ...]] = []
    for entry in entries:
        d = entry["data"]
        for series in d["series"]:
            yoy = {month: value for month, value in series["yoy"]}
            part = [
                (series["keyword"], month, value, _times100(yoy.get(month)), d["geo"], d["timeframe"],
                 d["source_name"], d["as_of"], d["source_url"],
                 f"trends:{d['geo']}:{series['keyword']}:{month}",
                 f"trends:{d['geo']}:{series['keyword']}:{month}:yoy" if yoy.get(month) is not None else None)
                for month, value in series["monthly"]
            ]
            rows += part
            short += part[-12:]
    notes = ["Trends 指数是相对热度,不是销量;同比 = 当月 / 12 个月前同月 - 1,无基期时为空"]
    if ext.get("trends_consistency"):
        notes.append("各次导入的地域/时间范围/关键词口径不一致,不可画在同一图上:" + "；".join(ext["trends_consistency"]))
    return ExtTable("外部_趋势", "Google Trends", headers, kinds, tuple(rows),
                    tuple(short) if len(short) < len(rows) else None, tuple(notes))


def _excerpts(ext: dict[str, Any]) -> ExtTable:
    entries = _entries(ext, "report-excerpts")
    headers = ("证据ID", "摘录", "状态", "来源", "日期", "来源链接")
    kinds = ("text",) * 6
    if not entries:
        return ExtTable("外部_研报摘录", "研报摘录", headers, kinds, None)
    rows = tuple(
        (i["claim_id"], i["value"], "可用" if i["status"] == "present" else "来源不可访问(inaccessible)",
         i["source_name"], i["as_of"], i["source_url"])
        for e in entries for i in e["data"]
    )
    return ExtTable("外部_研报摘录", "研报摘录", headers, kinds, rows,
                    notes=("摘录为人工录入的原文片段;来源不可访问的条目不带数值,只登记来源",))


def _tiktok_units(ext: dict[str, Any]) -> ExtTable:
    headers = ("口径(geo/segment)", "期间", "TikTok 台数", "全渠道台数", "TikTok 份额", "性质", "来源", "取数日期", "证据ID")
    kinds = ("text", "text", "num", "num", "pct", "text", "text", "text", "text")
    analyses = ext.get("tiktok_analysis") or []
    if not analyses:
        return ExtTable("外部_TikTok台数", "TikTok 渠道台数与份额", headers, kinds, None)
    lookup = _source_lookup(ext)
    rows: list[tuple[object, ...]] = []
    notes = ["单位:台(units),不含 GMV;份额 = TikTok 台数 / 全渠道台数(同 geo、segment、期间)"]
    for a in analyses:
        label = f"{a['geo']}/{a['segment']}"
        by_period: dict[str, tuple[Any, Any, Any, bool, list[str]]] = {}
        for s in a["shares"]:
            by_period[s["period"]] = (s["tiktok_units"], s["total_units"], _times100(s["share"]), s["is_forecast"], s["evidence_ids"])
        for m in a["monthly_2026"]:
            by_period.setdefault(m["month"], (m["units"], None, None, m["is_forecast"], [m["evidence_id"]]))
        for year, value in a["annual"]:
            by_period.setdefault(str(year), (value["units"], None, None, value["is_forecast"], value["evidence_ids"]))
        for period in sorted(by_period):
            tiktok, total, share, is_fc, ids = by_period[period]
            name, day = _sources_for(lookup, ids)
            rows.append((label, period, tiktok, total, share, _tag(is_fc), name, day, ", ".join(dict.fromkeys(ids))))
        if a["missing_months_2026"]:
            notes.append(f"{label} 2026 逐月数据未提供(不当 0)" if len(a["missing_months_2026"]) == 12
                         else f"{label} 缺失 2026 月份(不当 0):" + "、".join(a["missing_months_2026"]))
    return ExtTable("外部_TikTok台数", "TikTok 渠道台数与份额", headers, kinds, tuple(rows), notes=tuple(notes))


def _tiktok_cagr(ext: dict[str, Any]) -> ExtTable:
    headers = ("口径(geo/segment)", "状态", "起点(2024)台数", "终点(2026)台数", "CAGR", "性质", "方法", "来源", "取数日期", "证据ID", "未算出原因")
    kinds = ("text", "text", "num", "num", "chg", "text", "text", "text", "text", "text", "text")
    analyses = ext.get("tiktok_analysis") or []
    if not analyses:
        return ExtTable("外部_TikTokCAGR", "TikTok 台数 CAGR(2024-2026)", headers, kinds, None)
    lookup = _source_lookup(ext)
    rows = []
    for a in analyses:
        c = a["cagr_2024_2026"]
        name, day = _sources_for(lookup, c["evidence_ids"])
        rows.append((f"{a['geo']}/{a['segment']}", "已计算" if c["status"] == "ok" else "证据不足", c["start_units"],
                     c["end_units"], _times100(c["cagr"]), _tag(c["is_forecast"]) if c["status"] == "ok" else "-",
                     c["method"], name, day, ", ".join(c["evidence_ids"]), c["missing_reason"]))
    return ExtTable("外部_TikTokCAGR", "TikTok 台数 CAGR(2024-2026)", headers, kinds, tuple(rows),
                    notes=("证据不足时不产出 CAGR,也不由系统补数",))


def _tiktok_projection(ext: dict[str, Any]) -> ExtTable:
    headers = ("口径(geo/segment)", "年份", "情景", "预测台数", "年增速", "性质", "来源", "取数日期", "证据ID")
    kinds = ("text", "text", "text", "num", "chg", "text", "text", "text", "text")
    analyses = ext.get("tiktok_analysis") or []
    if not analyses:
        return ExtTable("外部_TikTok预测", "TikTok 台数预测(2027-2028 外推)", headers, kinds, None)
    lookup = _source_lookup(ext)
    rows: list[tuple[object, ...]] = []
    notes: list[str] = ["以下全部是外推预测,以 2026 年台数为基数、按 2024-2026 CAGR(中情景)与调用方给定的低/高增速复利外推"]
    for a in analyses:
        p = a["projection"]
        label = f"{a['geo']}/{a['segment']}"
        name, day = _sources_for(lookup, p["evidence_ids"])
        for point in p["points"]:
            rows.append((label, str(point["year"]), _SCENARIOS[point["scenario"]], point["units"],
                         _times100(point["growth_rate"]), FORECAST_TAG, name, day, ", ".join(p["evidence_ids"])))
        notes += [f"{label} 假设:" + _assumption(text) for text in p["assumptions"]]
    return ExtTable("外部_TikTok预测", "TikTok 台数预测(2027-2028 外推)", headers, kinds, tuple(rows), notes=tuple(notes))


def _forecast(ext: dict[str, Any]) -> ExtTable:
    headers = ("报告口径", "情景", "期间", "数值(台)", "性质", "来源", "取数日期", "证据ID")
    kinds = ("text", "text", "text", "num", "text", "text", "text", "text")
    forecast = ext.get("forecast")
    if not forecast:
        return ExtTable("外部_市场预测", "市场规模预测(基于本系统 Top 样本)", headers, kinds, None)
    r = forecast["result"]
    ev = forecast["evidence"]
    scope = _SCOPE_LABELS.get(r["scope"], r["scope"])
    widened = r["scope"] != r["source_scope"]
    ids = ", ".join(r["evidence_ids"])
    rows = [(scope, "基期", r["base_period"], r["base_value"], CONVERTED_TAG if widened else OBSERVED_TAG,
             ev["source_name"], ev["as_of"], ids)]
    for name in ("low", "mid", "high"):
        for point in r["scenarios"].get(name, []):
            rows.append((scope, _SCENARIOS[name], point["period"], point["value"], FORECAST_TAG,
                         ev["source_name"], ev["as_of"], ids))
    notes = [
        f"来源口径 {_SCOPE_LABELS.get(r['source_scope'], r['source_scope'])} → 报告口径 {scope};方法 {r['method']};预测跨度 {r['horizon']};单位:台/期",
        "Top 样本销量不等于渠道或全市场销量;跨口径只能经带来源的覆盖率假设换算",
        *(f"假设:{_assumption(text)}" for text in r["assumptions"]),
    ]
    return ExtTable("外部_市场预测", "市场规模预测(基于本系统 Top 样本)", headers, kinds, tuple(rows), notes=tuple(notes))


def _cross_channel(ext: dict[str, Any]) -> ExtTable:
    headers = ("期间", "Amazon 台数", "Amazon 性质", "TikTok 台数", "TikTok 性质", "来源", "取数日期", "证据ID")
    kinds = ("text", "num", "text", "num", "text", "text", "text", "text")
    doc = ext.get("cross_channel")
    if not doc:
        return ExtTable("外部_跨渠道", "跨渠道事实整合(Amazon 与 TikTok,同单位:台)", headers, kinds, None)
    summary = doc["summary"]
    lookup = _source_lookup(ext)
    rows = []
    for row in summary["table"]:
        name, day = _sources_for(lookup, row["evidence_ids"])
        rows.append((row["period"], row["amazon_units"], _tag(row["amazon_is_forecast"]), row["tiktok_units"],
                     _tag(row["tiktok_is_forecast"]), name, day, ", ".join(row["evidence_ids"])))
    notes = [f"口径:Amazon 侧 {summary['amazon_scope']};TikTok 侧 {summary['tiktok_scope']};单位:台",
             *(f"限制:{text}" for text in summary["limitations"]),
             f"人工决策栏:{summary['human_decision_slot']}"]
    return ExtTable("外部_跨渠道", "跨渠道事实整合(Amazon 与 TikTok,同单位:台)", headers, kinds, tuple(rows), notes=tuple(notes))


def _patent_logs(ext: dict[str, Any]) -> ExtTable:
    headers = ("检索词", "数据库", "检索范围", "状态", "检索日期", "命中记录数")
    kinds = ("text",) * 5 + ("int",)
    entries = _entries(ext, "patent-search-log")
    if not entries:
        notes = (PATENT_DISCLAIMER, "未导入检索日志:无法区分「未检索」与「已检索-范围内未发现」")
        return ExtTable("外部_专利检索", "专利检索日志", headers, kinds, None, notes=notes)
    rows = tuple((log["query"], log["database"], log["scope"], log["status_label"], log["searched_at"], log["record_count"])
                 for e in entries for log in e["data"])
    notes = tuple(dict.fromkeys([PATENT_DISCLAIMER, *(n for e in entries for log in e["data"] for n in log["notes"])]))
    return ExtTable("外部_专利检索", "专利检索日志", headers, kinds, rows, notes=notes)


def _patents(ext: dict[str, Any]) -> ExtTable:
    headers = ("专利号", "标题", "关联 ASIN/产品", "申请日", "公开日", "检索词", "来源", "取数日期", "链接/文件")
    kinds = ("text",) * 9
    entries = _entries(ext, "patents")
    notes = (PATENT_DISCLAIMER,)
    if not entries:
        return ExtTable("外部_专利记录", "专利记录", headers, kinds, None, notes=notes)
    rows = tuple(
        (r["patent_no"], r["title"], r["related_asin"] or r["related_product"], r["filed_date"], r["publication_date"],
         r["search_query"], r["evidence"]["source_name"], r["evidence"]["retrieved_at"], _evidence_locator(r["evidence"]))
        for e in entries for r in e["data"]
    )
    return ExtTable("外部_专利记录", "专利记录", headers, kinds, rows, notes=notes)


def _ads(ext: dict[str, Any]) -> ExtTable:
    headers = ("ASIN", "样本类型", "人工指定者", "流量结构时间范围", "流量结构数据", "评价时间范围", "12个月留评率",
               "留评率说明", "CPC 时间范围", "CPC 值", "币种", "来源", "取数日期", "备注")
    kinds = ("text",) * 6 + ("pct", "text", "text", "num", "text", "text", "text", "text")
    entries = _entries(ext, "ads")
    if not entries:
        return ExtTable("外部_广告", "广告 / 流量结构 / 留评率 / CPC", headers, kinds, None)
    rows = []
    for e in entries:
        for r in e["data"]:
            blocks = [b for b in (r["traffic"], r["reviews"], r["cpc"]) if b]
            rate = r["reviews"]["review_rate_12m"] if r["reviews"] else None
            rows.append((
                r["asin"], _COHORTS.get(r["cohort"], r["cohort"]), r["selected_by"],
                r["traffic"]["time_range"] if r["traffic"] else None,
                json.dumps(r["traffic"]["data"], ensure_ascii=False) if r["traffic"] else None,
                r["reviews"]["time_range"] if r["reviews"] else None,
                _times100(rate["rate"]) if rate else None, rate["note"] if rate else None,
                r["cpc"]["time_range"] if r["cpc"] else None, r["cpc"]["value"] if r["cpc"] else None,
                r["cpc"]["currency"] if r["cpc"] else None,
                "、".join(dict.fromkeys(b["evidence"]["source_name"] for b in blocks)) or None,
                "、".join(dict.fromkeys(b["evidence"]["retrieved_at"] for b in blocks)) or None,
                ";".join(r["notes"]),
            ))
    return ExtTable("外部_广告", "广告 / 流量结构 / 留评率 / CPC", headers, kinds, tuple(rows),
                    notes=("ASIN 由人工指定;留评率 = 近 12 个月新增评价合计 / 销量合计,缺月或缺值时不计算",))


def _big_brand(ext: dict[str, Any]) -> ExtTable:
    headers = ("品牌", "产品类型", "核查状态", "ABA 排名", "ABA 阈值", "ASIN 数", "合计月销量", "缺销量 ASIN 数",
               "最早上架 ASIN", "最早上架日期", "来源", "取数日期", "链接/文件", "说明", "人工解读栏")
    kinds = ("text", "text", "text", "int", "int", "int", "num", "int", "text", "text", "text", "text", "text", "text", "text")
    entries = _entries(ext, "big-brand")
    if not entries:
        return ExtTable("外部_大品牌", "大品牌核查", headers, kinds, None)
    rows = tuple(
        (d["brand"], d["product_type"], _BRAND_STATUS.get(d["status"], d["status"]), d["aba_rank"], d["aba_rank_threshold"],
         d["asin_count"], d["total_monthly_sales"], d["sales_missing_count"], d["earliest_asin"], d["earliest_listed_date"],
         d["evidence"]["source_name"], d["evidence"]["retrieved_at"], _evidence_locator(d["evidence"]),
         " ".join(d["notes"]), "由人工填写")
        for d in (e["data"] for e in entries)
    )
    return ExtTable("外部_大品牌", "大品牌核查", headers, kinds, rows,
                    notes=("核查状态只按 ABA 排名阈值这一条规则给出;有无大品牌的业务含义由人工解读",))


def _offsite(ext: dict[str, Any]) -> ExtTable:
    headers = ("品牌", "产品词", "检索词", "引擎/工具", "状态", "检索日期", "发现数", "发现明细(渠道: 链接)")
    kinds = ("text",) * 6 + ("int", "text")
    entries = _entries(ext, "offsite")
    if not entries:
        return ExtTable("外部_站外", "站外证据", headers, kinds, None)
    rows = []
    notes: list[str] = []
    for e in entries:
        for ledger in e["data"]:
            notes += ledger["notes"]
            for s in ledger["searches"]:
                rows.append((s["brand"], s["product_term"], s["query"], s["engine_tool"], s["status_label"], s["searched_at"],
                             len(s["findings"]), "；".join(f"{f['channel_type']}: {f['url']}" for f in s["findings"]) or None))
    return ExtTable("外部_站外", "站外证据", headers, kinds, tuple(rows), notes=tuple(dict.fromkeys(notes)))


def _cross_category(ext: dict[str, Any]) -> ExtTable:
    headers = ("品牌", "类目", "ASIN 数", "合计月销量", "缺销量 ASIN 数", "最早上架", "最晚上架", "按年上架数", "来源", "取数日期", "链接/文件")
    kinds = ("text", "text", "int", "num", "int", "text", "text", "text", "text", "text", "text")
    entries = _entries(ext, "cross-category")
    if not entries:
        return ExtTable("外部_跨类目", "同品牌跨类目 ASIN", headers, kinds, None)
    rows = tuple(
        (d.get("brand"), c["category"], c["asin_count"], c["total_monthly_sales"], c["sales_missing_count"],
         c["earliest_listed_date"], c["latest_listed_date"], json.dumps(c["listings_by_year"], ensure_ascii=False),
         d["evidence"]["source_name"], d["evidence"]["retrieved_at"], _evidence_locator(d["evidence"]))
        for d in (e["data"] for e in entries) for c in d["categories"]
    )
    notes = tuple(dict.fromkeys(n for e in entries for n in e["data"]["notes"]))
    return ExtTable("外部_跨类目", "同品牌跨类目 ASIN", headers, kinds, rows,
                    notes=(*notes, "卖家成色(已推爆/待推爆/持续上新)由人工判断,此处仅汇总事实"))


def _demand_source(d: dict[str, Any]) -> tuple[object, ...]:
    ev = d["evidence"]
    return (ev["source_name"], ev["retrieved_at"], ev.get("scope"), _evidence_locator(ev))


_DEMAND_SOURCE_HEADERS = ("来源", "取数日期", "取数范围", "链接/文件")
_DEMAND_SOURCE_KINDS = ("text",) * 4


def _keywords(ext: dict[str, Any]) -> ExtTable:
    headers = ("关键词", "细分(导入标注)", "最新月份", "最新搜索量", "上期月份", "上期搜索量", "环比%", "已观测月数", *_DEMAND_SOURCE_HEADERS)
    kinds = ("text", "text", "text", "num", "text", "num", "chg", "int", *_DEMAND_SOURCE_KINDS)
    entries = _entries(ext, "keywords")
    if not entries:
        return ExtTable("外部_关键词", "关键词搜索量", headers, kinds, None)
    rows = tuple(
        (r["keyword"], r["segment"], r["latest_month"], r["latest_search_volume"], r["previous_month"],
         r["previous_search_volume"], r["change_pct"], r["months_observed"], *_demand_source(d))
        for d in (e["data"] for e in entries) for r in d["summary"]["table"]
    )
    notes = tuple(dict.fromkeys(n for e in entries for n in e["data"]["summary"]["notes"]))
    return ExtTable("外部_关键词", "关键词搜索量", headers, kinds, rows, notes=notes)


def _scenarios(ext: dict[str, Any]) -> ExtTable:
    headers = ("场景", "提及数", "占比%", "缺提及数行数", "涉及细分", "示例", *_DEMAND_SOURCE_HEADERS)
    kinds = ("text", "num", "pct", "int", "text", "text", *_DEMAND_SOURCE_KINDS)
    entries = _entries(ext, "scenarios")
    if not entries:
        return ExtTable("外部_场景", "使用场景", headers, kinds, None)
    rows = tuple(
        (r["scenario"], r["mentions"], r["share_pct"], r["missing_count_rows"], "、".join(r["segments"]) or None,
         r["example"], *_demand_source(d))
        for d in (e["data"] for e in entries) for r in d["summary"]["table"]
    )
    notes = tuple(dict.fromkeys(n for e in entries for n in e["data"]["summary"]["notes"]))
    return ExtTable("外部_场景", "使用场景", headers, kinds, rows, notes=notes)


def _voc(ext: dict[str, Any]) -> ExtTable:
    headers = ("主题", "摘录条数", "占比%", "正面", "负面", "中性", "负面占比%", "代表摘录(情感: 原文 | 出处)", *_DEMAND_SOURCE_HEADERS)
    kinds = ("text", "int", "pct", "int", "int", "int", "pct", "text", *_DEMAND_SOURCE_KINDS)
    entries = _entries(ext, "voc")
    if not entries:
        return ExtTable("外部_VOC", "用户评论(VOC)主题", headers, kinds, None)
    rows = tuple(
        (r["theme"], r["excerpts"], r["share_pct"], r["positive"], r["negative"], r["neutral"], r["negative_share_pct"],
         "；".join(f"{x['sentiment']}: {x['excerpt']} | {x['locator'] or x['asin'] or x['platform'] or '-'}" for x in r["examples"]),
         *_demand_source(d))
        for d in (e["data"] for e in entries) for r in d["summary"]["table"]
    )
    notes = tuple(dict.fromkeys(n for e in entries for n in e["data"]["summary"]["notes"]))
    return ExtTable("外部_VOC", "用户评论(VOC)主题", headers, kinds, rows, notes=notes)


def external_tables(ext: dict[str, Any]) -> list[ExtTable]:
    builders = (_trends, _excerpts, _tiktok_units, _tiktok_cagr, _tiktok_projection, _forecast, _cross_channel,
                _patent_logs, _patents, _ads, _big_brand, _offsite, _cross_category, _keywords, _scenarios, _voc)
    return [build(ext) for build in builders]


# ------------------------------------------------------------------ 渲染


def external_markdown(ext: dict[str, Any]) -> list[str]:
    lines = ["", f"## {TITLE}", "", *[f"- {note}" for note in INTRO_NOTES], ""]
    lines += ["### 缺失与不可访问(醒目)", ""]
    gaps = external_gaps(ext)
    lines += [f"- **{gap}**" for gap in gaps] or ["- 无已登记缺口。"]
    for table in external_tables(ext):
        block = table.to_block(inline_notes=False)
        lines.append("")
        if block.headers:
            lines += sections.markdown_table(block)
        else:
            lines += [f"### {block.title}", "", block.note]
        if table.notes:
            lines += [""] + [f"- {note}" for note in table.notes]
    return lines


def external_dashboard_blocks(ext: dict[str, Any]) -> list[sections.TableBlock]:
    return [table.to_block() for table in external_tables(ext)]
