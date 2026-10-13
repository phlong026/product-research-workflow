"""外部证据槽位：每个研究模块需要哪些证据、单位约束、可用的数据来源提示。

槽位只定义“需要什么证据”，不代表系统已能自动获取——来源提示仅供执行研究的人/AI 参考。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlotSpec:
    key: str
    module: str
    title: str
    numeric: bool = False
    units: tuple[str, ...] = ()  # 空=不限制
    provider_hint: str = ""


UNITS_ONLY = ("units",)  # 原需求要求“台数”，不能拿 GMV/金额替代

_SPECS = [
    # --- 市场规模与增速（首期最高优先级） ---
    SlotSpec("market_size_us", "market_growth", "美国整体市场规模", True, ("units", "usd"), "行业研报 / 公开统计"),
    SlotSpec("market_forecast", "market_growth", "未来增速与市场预测", False, (), "行业研报"),
    SlotSpec("industry_report", "market_growth", "行业研究报告要点", False, (), "公开研报"),
    SlotSpec("google_trends", "market_growth", "Google Trends 趋势", False, (), "Google Trends / Apify"),
    SlotSpec("seasonality_cause", "market_growth", "季节变化原因", False, (), "西柚关键词季节性 / Google Trends"),
    SlotSpec("product_iteration", "market_growth", "产品迭代情况", False, (), "评论 / 新品页面 / 站外资讯"),
    # --- 三类市场深挖 ---
    SlotSpec("channel_erosion", "market_deep_dive", "渠道侵蚀（站外/其他渠道）", False, (), "站外渠道调研"),
    SlotSpec("substitutes", "market_deep_dive", "替代品", False, (), "品类对比 / 评论"),
    SlotSpec("growth_drivers", "market_deep_dive", "增长驱动", False, (), "趋势 / 新品 / 场景调研"),
    SlotSpec("non_essential_sustainability", "market_deep_dive", "非刚需持续性", False, (), "趋势 / 复购 / 评论"),
    # --- TikTok 与 Amazon 综合（单位必须是台数） ---
    SlotSpec("tiktok_units_global_overall", "tiktok_amazon", "TikTok 全球整体销量（台数）", True, UNITS_ONLY, "出海匠"),
    SlotSpec("tiktok_units_us_overall", "tiktok_amazon", "TikTok 美国整体销量（台数）", True, UNITS_ONLY, "出海匠"),
    SlotSpec("tiktok_units_global_segment", "tiktok_amazon", "TikTok 全球细分销量（台数）", True, UNITS_ONLY, "出海匠"),
    SlotSpec("tiktok_units_us_segment", "tiktok_amazon", "TikTok 美国细分销量（台数）", True, UNITS_ONLY, "出海匠"),
    SlotSpec("tiktok_share", "tiktok_amazon", "TikTok 占 Amazon+TikTok 的占比", True, ("pct",), "出海匠 + Amazon 数据"),
    SlotSpec("tiktok_history_growth", "tiktok_amazon", "TikTok 历史增长", False, (), "出海匠"),
    SlotSpec("tiktok_forecast", "tiktok_amazon", "TikTok 未来预测", False, (), "出海匠 / 研报"),
    SlotSpec("cross_channel_conclusion", "tiktok_amazon", "跨渠道综合观察", False, (), "综合"),
    # --- 竞争深度研究 ---
    SlotSpec("patents", "competition_deep", "专利资料", False, (), "专利库"),
    SlotSpec("ad_types_traffic_structure", "competition_deep", "广告类型与流量结构", False, (), "SIF / 西柚"),
    SlotSpec("cpc", "competition_deep", "CPC 证据", True, ("usd",), "SIF / 西柚"),
    SlotSpec("review_rate", "competition_deep", "留评率", True, ("pct",), "评论数据"),
    SlotSpec("big_brand_entry", "competition_deep", "大品牌进入情况", False, (), "品牌调研"),
    SlotSpec("offsite_channels", "competition_deep", "站外渠道", False, (), "Apify 公开网页"),
    SlotSpec("seller_cross_category", "competition_deep", "卖家跨类目商品清单", False, (), "卖家精灵 / Apify"),
]

SLOTS: dict[str, SlotSpec] = {spec.key: spec for spec in _SPECS}

# 首期必须覆盖的基线模块（原需求最高优先级）；其余模块由人工确认的研究路线触发。
BASELINE_MODULES = ("market_growth", "tiktok_amazon", "competition_deep")

# 路由器 next_modules -> 证据模块。空元组表示该模块由已计算的确定性分析覆盖，无需外部证据。
ROUTE_MODULE_TO_SLOTS: dict[str, tuple[str, ...]] = {
    "seasonality": ("seasonality_cause",),
    "google_trends": ("google_trends",),
    "external_channels": ("channel_erosion", "offsite_channels"),
    "replacement_research": ("substitutes",),
    "trend_sustainability": ("non_essential_sustainability",),
    "segment_deep_dive": ("growth_drivers",),
    "brand_deep_dive": ("big_brand_entry",),
    "advertising_research": ("ad_types_traffic_structure", "cpc"),
    "new_product_brands": (),
    "vc_sc_comparison": (),
    "manual_research": (),
}

MODULE_TITLES = {
    "market_growth": "市场规模与增速",
    "market_deep_dive": "市场深挖（路线触发）",
    "tiktok_amazon": "TikTok 与 Amazon 综合",
    "competition_deep": "竞争深度研究",
}


def required_slots(confirmed_route_modules: list[str] | None = None) -> list[SlotSpec]:
    """基线槽位 + 已确认路线触发的槽位，去重且顺序稳定。"""
    keys: list[str] = [s.key for s in _SPECS if s.module in BASELINE_MODULES]
    for module in confirmed_route_modules or []:
        for key in ROUTE_MODULE_TO_SLOTS.get(module, ()):
            if key not in keys:
                keys.append(key)
    return [SLOTS[k] for k in keys]
