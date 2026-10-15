"""T2-8 证据驱动的深挖执行。

对人工确认的研究路线(或显式 --modules),把「已算指标 + 已登记外部证据」汇总成可追溯的事实表,
并逐项标出还缺哪些证据。纯确定性、不联网、不调模型:
- 不设内置阈值,不判断「好/坏/该不该进入」,只陈述事实、方向与缺口;
- 缺证据就是缺口,不拿默认值或 0 顶替;
- 系统生成的文案(标题/标签/缺口说明)过禁词守卫,用户导入的原文不在此列。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from research_workflow.competition_external.ledger import assert_no_decision_language

from .models import DeepDiveFact, DeepDiveReport, DeepDiveSection, EvidenceSlot

MAX_ROWS = 30  # 单节事实行数上限,超出在 limitations 披露(完整数据见各 JSON 工件)

# 模块 -> (主题, 标题);主题对应 T2-8 的几类深挖
THEMES = {
    "channel_erosion": "渠道侵蚀",
    "replacement": "替代品 / 细分迁移",
    "growth_driver": "增长驱动",
    "sustainability": "非刚需持续性",
    "head_suction": "头部虹吸",
    "demand": "需求侧(关键词 / 场景 / VOC)",
}


class Inputs:
    """深挖所需的全部输入(均为已落盘 JSON 的 dict 形态,便于离线测试)。"""

    def __init__(self, market: dict[str, Any], competition: dict[str, Any] | None = None,
                 new_products: dict[str, Any] | None = None, external: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> None:
        self.market = market
        self.competition = competition
        self.new_products = new_products
        self.external = external or {}
        self.context = context or {}

    def entries(self, kind: str) -> list[dict[str, Any]]:
        return list(self.external.get(kind) or [])


def _slot(inputs: Inputs, kind: str, label: str, missing_hint: str = "") -> EvidenceSlot:
    entries = inputs.entries(kind)
    if entries:
        records = sum(e.get("record_count", 0) for e in entries)
        return EvidenceSlot(kind, label, "present", f"已登记 {len(entries)} 次导入 / {records} 条记录")
    return EvidenceSlot(kind, label, "missing", missing_hint or "尚未提供;请 import-evidence")


def _cap(items: list[Any], limitations: list[str]) -> list[Any]:
    if len(items) > MAX_ROWS:
        limitations.append(f"仅列出前 {MAX_ROWS} 行(按影响量级排序),共 {len(items)} 行;完整数据见对应 JSON 工件")
        return items[:MAX_ROWS]
    return items


def _section(module: str, theme: str, title: str, facts: list[DeepDiveFact], slots: list[EvidenceSlot],
             limitations: list[str]) -> DeepDiveSection:
    return DeepDiveSection(module, THEMES[theme], title, tuple(facts), tuple(slots), tuple(limitations))


# ------------------------------------------------------------------ 各模块


def _seasonality(inputs: Inputs) -> DeepDiveSection:
    season = inputs.market.get("seasonality") or {}
    facts: list[DeepDiveFact] = []
    limitations: list[str] = ["季节性只描述已观测的完整年份,不外推;原因归因需另行验证"]
    for year in season.get("years", []):
        evidence = (f"market:seasonality:{year['year']}",)
        if year["is_complete"]:
            facts.append(DeepDiveFact(f"season:{year['year']}", f"{year['year']} 年高/低月",
                                      f"高 {year['high_month']}({year['high_sales']:,}) / 低 {year['low_month']}({year['low_sales']:,})",
                                      "月销量", evidence))
        else:
            facts.append(DeepDiveFact(f"season:{year['year']}", f"{year['year']} 年", f"仅观测 {year['months_observed']} 个月,不判高低月",
                                      None, evidence, note="、".join(year.get("missing_months", [])) or None))
    if season.get("recurring_high_month") or season.get("recurring_low_month"):
        facts.append(DeepDiveFact("season:recurring", "每个完整年份都重复的高/低月",
                                  f"高 {season.get('recurring_high_month') or '-'} / 低 {season.get('recurring_low_month') or '-'}",
                                  None, ("market:seasonality",)))
    if season.get("coverage_warning"):
        limitations.append(f"覆盖提示: {season['coverage_warning']}")
    if not season.get("complete_years"):
        limitations.append("没有 12 个月齐全的年份:不能给出季节性高低月")
    return _section("seasonality", "sustainability", "季节性", facts,
                    [_slot(inputs, "trends", "Google Trends 导出(季节性交叉验证)")], limitations)


def _segment_deep_dive(inputs: Inputs, module: str = "segment_deep_dive") -> DeepDiveSection:
    limitations: list[str] = ["份额变化与贡献量为算术分解,不等于原因;替代关系需结合下列外部证据由人工判断"]
    rows = sorted(inputs.market.get("decompositions", []), key=lambda r: (-abs(r["share_contribution_sales"]), r["segment"], r["base_month"]))
    facts: list[DeepDiveFact] = []
    for row in _cap(rows, limitations):
        market_change, segment_change = row["market_change_pct"], row["segment_change_pct"]
        if market_change is None or segment_change is None:
            direction = "缺基期,不判方向"
        elif market_change < 0 < segment_change:
            direction = "大盘下降而该细分增长"
        elif segment_change < 0 < market_change:
            direction = "大盘增长而该细分下降"
        else:
            direction = "与大盘同向"
        evidence = (f"decomp:{row['base_month']}:{row['current_month']}:{row['segment']}",)
        facts.append(DeepDiveFact(
            evidence[0], f"{row['segment']}:{row['base_month']}→{row['current_month']}",
            f"销量 {row['base_segment_sales']:,}→{row['current_segment_sales']:,};份额 {row['share_change_pp']:+.2f}pp;"
            f"份额效应贡献 {row['share_contribution_sales']:+,.0f};{direction}", "月销量", evidence))
    if not rows:
        limitations.append("市场指标里没有细分分解(缺少可比的基期/现期)")
    return _section(module, "replacement", "细分迁移与替代线索", facts,
                    [_slot(inputs, "report-excerpts", "研报摘录(替代品/品类演变)"), _slot(inputs, "offsite", "站外证据"),
                     _slot(inputs, "cross-category", "同品牌跨类目 ASIN")], limitations)


def _group_facts(prefix: str, label: str, groups: list[dict[str, Any]]) -> list[DeepDiveFact]:
    ordered = sorted(groups, key=lambda g: (-g["share_of_successful_pct"], g["value"]))
    return [DeepDiveFact(f"new_products:{prefix}:{g['value']}", f"{label} {g['value']}",
                         f"新品 {g['total_products']} / 达标 {g['successful_products']}(占达标新品 {g['share_of_successful_pct']:.2f}%)",
                         "个", ("new_products:summary",)) for g in ordered]


def _new_products(inputs: Inputs, module: str = "new_products") -> DeepDiveSection:
    data = inputs.new_products
    slot = EvidenceSlot("new_products", "新品跟踪表指标", "present" if data else "missing",
                        "已计算" if data else (inputs.context.get("new_products_skipped_reason") or "未提供新品跟踪表"))
    if not data:
        return _section(module, "growth_driver", "新品对增长的贡献", [], [slot], ["没有新品指标:无法回答新品是否在推动增长"])
    summary = data["summary"]
    evidence = ("new_products:summary",)
    facts = [
        DeepDiveFact("np:total", "跟踪新品数", summary["total_products"], "个", evidence),
        DeepDiveFact("np:success", "launch success(下限 / 上限)",
                     f"{summary['successful_products']} / {summary['successful_products'] + summary['launch_success_unknown_products']}",
                     "个", evidence, note="缺月无法判定的新品只计入上限"),
        DeepDiveFact("np:retention", "留存率(下限 / 上限)",
                     f"{summary['retention_rate_pct']:.2f}% / {summary['retention_rate_upper_bound_pct']:.2f}%", None, evidence,
                     note=summary.get("retention_denominator_note") or None),
        DeepDiveFact("np:thresholds", "采用的阈值(人工给定)",
                     f"推起 {summary['promotion_waist_sales_threshold']} / 留存 {summary['retention_sales_threshold']}", "月销量", evidence),
    ]
    if module == "vc_sc_comparison":
        facts += [DeepDiveFact(f"new_products:account_type:{a['account_type']}", f"账号类型 {a['account_type']}",
                               f"新品 {a['total_products']} / 达标 {a['successful_products']}(占达标新品 {a['share_of_successful_pct']:.2f}%)",
                               "个", evidence) for a in sorted(data["account_types"], key=lambda a: (-a["share_of_successful_pct"], a["account_type"]))]
    else:
        facts += _group_facts("segment", "细分", data["segments"]) + _group_facts("region", "卖家地区", data["seller_regions"])
    return _section(module, "growth_driver", "新品对增长的贡献" if module != "vc_sc_comparison" else "VC / SC 新品对比",
                    facts, [slot], ["新品指标是否「推动了增长」需结合大盘与细分变化由人工判断;此处只陈述新品侧事实"])


def _trend_sustainability(inputs: Inputs) -> DeepDiveSection:
    market = sorted(inputs.market.get("market", []), key=lambda r: r["month"])
    facts: list[DeepDiveFact] = []
    limitations = ["持续性无法由单一指标证明;下列为同比方向的事实计数与外部证据状态,不外推、不下判断"]
    with_yoy = [r for r in market if r.get("yoy_pct") is not None]
    if with_yoy:
        negative = [r for r in with_yoy if r["yoy_pct"] < 0]
        facts.append(DeepDiveFact("sustain:yoy", "有同比的月份中同比为负的月数", f"{len(negative)} / {len(with_yoy)}", "月",
                                  tuple(f"market:{r['month']}" for r in with_yoy[-12:]),
                                  note=f"统计区间 {with_yoy[0]['month']}~{with_yoy[-1]['month']}"))
        latest = with_yoy[-1]
        facts.append(DeepDiveFact("sustain:latest", f"最近有同比的月份 {latest['month']}", f"{latest['yoy_pct']:+.2f}%", "同比", (f"market:{latest['month']}",)))
    else:
        limitations.append("没有任何月份具备同比基期:无法描述同比走势")
    for item in inputs.external.get("trends_consistency") or []:
        facts.append(DeepDiveFact(f"trends:{item.get('keyword', '-')}", "Google Trends 口径一致性检查", str(item), None, ("external:trends",)))
    forecast = inputs.external.get("forecast")
    if forecast:
        result = forecast["result"]
        facts.append(DeepDiveFact("sustain:forecast", f"外推预测({result['method']},预测值非观测)",
                                  f"基期 {result['base_period']}={result['base_value']:,.0f};情景 {sorted(result['scenarios'])}", "销量",
                                  tuple(forecast["params"]["evidence_ids"]), note="; ".join(result["assumptions"])))
    return _section("trend_sustainability", "sustainability", "趋势持续性线索", facts,
                    [_slot(inputs, "trends", "Google Trends 导出"),
                     EvidenceSlot("forecast", "市场规模外推预测", "present" if forecast else "missing",
                                  "" if forecast else "尚未运行 build-market-forecast")], limitations)


def _external_channels(inputs: Inputs) -> DeepDiveSection:
    facts: list[DeepDiveFact] = []
    limitations = ["渠道侵蚀需要同口径的 Amazon 与其它渠道数据;口径不同的数字不做差值,仅并列呈现"]
    cross = inputs.external.get("cross_channel")
    if cross:
        summary = cross["summary"]
        limitations += list(summary.get("limitations", []))
        for row in summary["table"]:
            facts.append(DeepDiveFact(f"channel:{row.get('period', '-')}", f"跨渠道 {cross['geo']}/{cross['segment']} {row.get('period', '-')}",
                                      "; ".join(f"{k}={v}" for k, v in row.items() if k != "period"), summary["unit"],
                                      tuple(summary["evidence_ids"])))
    for analysis in inputs.external.get("tiktok_analysis") or []:
        cagr = analysis["cagr_2024_2026"]
        facts.append(DeepDiveFact(f"tiktok:{analysis['geo']}:{analysis['segment']}", f"TikTok {analysis['geo']}/{analysis['segment']} CAGR 2024-2026",
                                  f"{cagr['value']:.4f}" if cagr.get("status") == "ok" and cagr.get("value") is not None else f"证据不足: {cagr.get('missing_reason')}",
                                  None, ("external:tiktok-units",)))
    offsite = [s for e in inputs.entries("offsite") for ledger in e["data"] for s in ledger["searches"]]
    if offsite:
        facts.append(DeepDiveFact("channel:offsite", "站外检索次数(已检索-发现 / 已检索-未发现 / 未检索)",
                                  " / ".join(str(sum(1 for s in offsite if s["status"] == st)) for st in ("found", "not_found", "not_searched")),
                                  "次", ("external:offsite",), note="「未检索」不等于「未发现」"))
    return _section("external_channels", "channel_erosion", "渠道迁移线索", facts,
                    [_slot(inputs, "tiktok-units", "TikTok 渠道台数"),
                     EvidenceSlot("cross_channel", "Amazon×TikTok 事实整合", "present" if cross else "missing",
                                  "" if cross else "尚未运行 build-cross-channel"),
                     _slot(inputs, "offsite", "站外证据"), _slot(inputs, "report-excerpts", "研报摘录")], limitations)


def _head_suction(inputs: Inputs, module: str = "brand_deep_dive") -> DeepDiveSection:
    facts: list[DeepDiveFact] = []
    limitations = ["头部变化的原因(品牌力、广告、价格)需外部证据支持;以下只陈述指标与已登记证据"]
    competition = inputs.competition or {}
    for signal in sorted(competition.get("head_suction", []), key=lambda s: s["month"]):
        evidence = (f"competition:head_suction:{signal['month']}:{signal['head_cutoff']}:{signal['broad_cutoff']}",)
        facts.append(DeepDiveFact(evidence[0], f"{signal['month']} Top{signal['head_cutoff']} vs Top{signal['broad_cutoff']} 同比",
                                  f"{signal['head_yoy_pct']}% vs {signal['broad_yoy_pct']}%;形态 {signal['pattern']}", "同比", evidence))
    conc = sorted(competition.get("concentration", []), key=lambda r: r["month"])
    if conc:
        last = conc[-1]
        facts.append(DeepDiveFact("conc:latest", f"{last['month']} 集中度(Top3/Top10 品牌)",
                                  f"{last['top3_brand_share_pct']:.2f}% / {last['top10_brand_share_pct']:.2f}%", "销量份额", (f"competition:concentration:{last['month']}",)))
    for entry in inputs.entries("big-brand"):
        d = entry["data"]
        facts.append(DeepDiveFact(f"big_brand:{d['brand']}", f"大品牌核查 {d['brand']}/{d['product_type']}", d["status"], None, ("external:big-brand",),
                                  note="仅按 ABA 排名阈值这一条规则"))
    ad_records = [r for e in inputs.entries("ads") for r in e["data"]]
    if ad_records:
        facts.append(DeepDiveFact("ads:count", "人工指定的广告/流量证据 ASIN 数", len(ad_records), "个", ("external:ads",)))
    logs = [log for e in inputs.entries("patent-search-log") for log in e["data"]]
    if logs:
        facts.append(DeepDiveFact("patent:logs", "专利检索(已检索-发现 / 范围内未发现 / 未检索)",
                                  " / ".join(str(sum(1 for log in logs if log["status"] == st)) for st in ("searched_found", "searched_none", "not_searched")),
                                  "条", ("external:patent-search-log",), note="仅检索记录,不构成法律意见"))
    if not competition:
        limitations.append("没有竞争指标(competition.json)")
    title = "头部集中与虹吸线索" if module == "brand_deep_dive" else "广告 / 流量证据"
    return _section(module, "head_suction", title, facts,
                    [_slot(inputs, "big-brand", "大品牌核查"), _slot(inputs, "ads", "广告/流量/评价/CPC 证据"),
                     _slot(inputs, "patents", "专利记录"), _slot(inputs, "patent-search-log", "专利检索日志"),
                     _slot(inputs, "offsite", "站外证据")], limitations)


def _demand(kind: str, module: str, title: str, summary_label: str):
    def build(inputs: Inputs) -> DeepDiveSection:
        facts: list[DeepDiveFact] = []
        limitations = ["标签与计数来自导入文件(人工或外部 AI 标注);样本不等于总体,取数范围见各条登记"]
        rows: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        for entry in inputs.entries(kind):
            for row in entry["data"]["summary"]["table"]:
                rows.append((row, entry["data"]["evidence"], entry["seq"]))
        # 多次导入合并后按该类别的影响量级全局排序(未知排最后),再截断:后一次导入的大项不能被前一次的小项挤掉
        impact = {"keywords": "latest_search_volume", "scenarios": "mentions", "voc": "excerpts"}[kind]
        label_key = {"keywords": "keyword", "scenarios": "scenario", "voc": "theme"}[kind]
        rows.sort(key=lambda item: (item[0][impact] is None, -(item[0][impact] or 0), item[2], str(item[0][label_key]).lower()))
        for row, evidence, seq in _cap(rows, limitations):
            ids = (f"external:{kind}:{seq}",)
            if kind == "keywords":
                value = (f"搜索量 {row['latest_search_volume']:,.0f}({row['latest_month']})" if row["latest_search_volume"] is not None else "缺搜索量") + \
                        (f";较上期 {row['change_pct']:+.2f}%" if row["change_pct"] is not None else "")
                label = row["keyword"]
            elif kind == "scenarios":
                value = f"提及 {row['mentions']:g}(占 {row['share_pct']:.2f}%)" if row["mentions"] is not None else "缺提及数(未知,不是 0)"
                label = row["scenario"]
            else:
                value = f"{row['excerpts']} 条(占 {row['share_pct']:.2f}%);正面 {row['positive']} / 负面 {row['negative']} / 中性 {row['neutral']}"
                label = row["theme"]
            facts.append(DeepDiveFact(f"{kind}:{seq}:{label}", label, value, None, ids, note=f"{evidence['source_name']} · {evidence['retrieved_at']} · 范围: {evidence.get('scope')}"))
        return _section(module, "demand", title, facts, [_slot(inputs, kind, summary_label)], limitations)
    return build


_BUILDERS: dict[str, Callable[[Inputs], DeepDiveSection]] = {
    "seasonality": _seasonality,
    "segment_deep_dive": _segment_deep_dive,
    "replacement_research": lambda i: _segment_deep_dive(i, "replacement_research"),
    "new_products": _new_products,
    "vc_sc_comparison": lambda i: _new_products(i, "vc_sc_comparison"),
    "trend_sustainability": _trend_sustainability,
    "google_trends": lambda i: _trend_sustainability(i),
    "external_channels": _external_channels,
    "brand_deep_dive": _head_suction,
    "advertising_research": lambda i: _head_suction(i, "advertising_research"),
    "keyword_research": _demand("keywords", "keyword_research", "关键词搜索量", "关键词搜索量"),
    "scenario_research": _demand("scenarios", "scenario_research", "使用场景", "使用场景"),
    "voc_research": _demand("voc", "voc_research", "用户评论(VOC)主题", "用户评论主题"),
}
_UNSUPPORTED = {
    "new_product_brands": "新品结果里没有品牌维度(只有细分/地区/账号类型),无法自动按品牌拆分;请人工补充品牌明细",
    "manual_research": "人工自选研究,无自动执行内容",
}
SUPPORTED_MODULES = tuple(_BUILDERS)


def _guard(sections: Iterable[DeepDiveSection]) -> None:
    """系统生成的固定文案(标题/槽位标签/限制说明/事实标签)不得越过人工决策边界。"""
    texts: list[str] = []
    for section in sections:
        texts += [section.title, section.theme, *section.limitations]
        texts += [slot.label for slot in section.slots]
        texts += [fact.label for fact in section.facts if fact.id.startswith(("season", "sustain", "np:", "conc", "ads:", "patent", "channel:offsite"))]
    assert_no_decision_language(texts)


def run_deep_dive(inputs: Inputs, *, confirmed_route_ids: list[str], modules: list[str]) -> DeepDiveReport:
    """modules 已由调用方从人工确认的路线与显式 --modules 合并去重;未知模块直接报错。"""
    unknown = [m for m in modules if m not in _BUILDERS and m not in _UNSUPPORTED]
    if unknown:
        raise ValueError(f"未知深挖模块: {unknown};可选: {sorted([*_BUILDERS, *_UNSUPPORTED])}")
    report = DeepDiveReport(list(confirmed_route_ids), list(modules))
    built: dict[str, DeepDiveSection] = {}
    for module in modules:
        if module in _UNSUPPORTED:
            report.unsupported.append({"module": module, "reason": _UNSUPPORTED[module]})
            continue
        section = _BUILDERS[module](inputs)
        # 同一底层分析被多个模块名映射(如 google_trends / trend_sustainability)时只保留一节
        built.setdefault(section.module, section)
    report.sections = list(built.values())
    _guard(report.sections)
    return report
