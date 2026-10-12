from __future__ import annotations

from research_workflow.competition.models import CompetitionMetricsResult
from research_workflow.metrics.models import MarketMetricsResult
from research_workflow.new_products.models import NewProductAnalysisResult
from research_workflow.workflow.service import WorkflowService

from .models import ResearchRoutePlan, RouteSuggestion, RouterConfig


class ResearchRouter:
    """Suggest the next *research* branch. It never produces a business decision."""

    def __init__(self, config: RouterConfig | None = None) -> None:
        self.config = config or RouterConfig()
        self.config.validate()

    def suggest(
        self,
        market: MarketMetricsResult,
        competition: CompetitionMetricsResult | None = None,
        new_products: NewProductAnalysisResult | None = None,
    ) -> ResearchRoutePlan:
        suggestions: list[RouteSuggestion] = []

        latest_market = sorted(market.market, key=lambda row: row.month)[-1] if market.market else None
        if latest_market and latest_market.yoy_pct is not None and latest_market.yoy_pct < 0:
            evidence = (f"market:{latest_market.month}",)
            suggestions.append(
                RouteSuggestion(
                    route_id="market_decline_deep_dive",
                    title="大盘缩量原因研究",
                    reason=(
                        f"{latest_market.month} 市场同比下降 {abs(latest_market.yoy_pct):.2f}%，"
                        "需要进一步区分季节性、渠道迁移、需求下降或产品替代。"
                    ),
                    evidence_ids=evidence,
                    next_modules=("seasonality", "external_channels", "replacement_research"),
                    priority="high",
                )
            )

        # A structural divergence is threshold-free: market < 0 while a segment > 0.
        for row in market.decompositions:
            if (
                row.market_change_pct is not None
                and row.segment_change_pct is not None
                and row.market_change_pct < 0 < row.segment_change_pct
            ):
                evidence_id = f"decomp:{row.base_month}:{row.current_month}:{row.segment}"
                suggestions.append(
                    RouteSuggestion(
                        route_id=f"segment_migration:{row.segment}",
                        title=f"细分迁移研究：{row.segment}",
                        reason=(
                            f"{row.segment} 在大盘下降期间仍增长，且份额变化 "
                            f"{row.share_change_pp:+.2f}pp；需要继续研究产品替代、"
                            "新品推动和站外渠道。"
                        ),
                        evidence_ids=(evidence_id,),
                        next_modules=("segment_deep_dive", "new_products", "external_channels"),
                        priority="high",
                    )
                )

        # Optional configurable branch: do not invent what "small" or "fast" means.
        if (
            latest_market
            and self.config.small_market_sales_max is not None
            and self.config.fast_growth_pct is not None
            and latest_market.total_sales <= self.config.small_market_sales_max
            and latest_market.yoy_pct is not None
            and latest_market.yoy_pct >= self.config.fast_growth_pct
        ):
            suggestions.append(
                RouteSuggestion(
                    route_id="small_market_fast_growth",
                    title="小市场高速增长研究",
                    reason=(
                        f"当前销量 {latest_market.total_sales} 不高于项目配置的小市场阈值 "
                        f"{self.config.small_market_sales_max}，同比 {latest_market.yoy_pct:.2f}% "
                        f"达到增长阈值 {self.config.fast_growth_pct:.2f}%。"
                    ),
                    evidence_ids=(f"market:{latest_market.month}",),
                    next_modules=("google_trends", "external_channels", "trend_sustainability"),
                    priority="normal",
                )
            )

        if competition:
            for signal in competition.head_suction:
                if signal.pattern == "head_up_broad_down":
                    suggestions.append(
                        RouteSuggestion(
                            route_id=f"head_suction:{signal.month}",
                            title="头部虹吸研究",
                            reason=(
                                f"{signal.month} Top{signal.head_cutoff} 同比增长，而 "
                                f"Top{signal.broad_cutoff} 整体同比下降，需要继续研究头部品牌、"
                                "广告/流量结构与腰尾部承压。"
                            ),
                            evidence_ids=(
                                f"competition:head_suction:{signal.month}:{signal.head_cutoff}:{signal.broad_cutoff}",
                            ),
                            next_modules=("brand_deep_dive", "advertising_research"),
                            priority="high",
                        )
                    )

        if new_products and new_products.summary.total_products:
            suggestions.append(
                RouteSuggestion(
                    route_id="new_product_structure_review",
                    title="新品结构复核",
                    reason=(
                        f"已跟踪 {new_products.summary.total_products} 个新品，其中 "
                        f"{new_products.summary.successful_products} 个满足项目定义的 launch_success。"
                    ),
                    evidence_ids=("new_products:summary",),
                    next_modules=("new_product_brands", "vc_sc_comparison"),
                    priority="normal",
                )
            )

        if not suggestions:
            suggestions.append(
                RouteSuggestion(
                    route_id="manual_research",
                    title="人工选择下一步研究",
                    reason="当前已计算指标未触发已配置的标准研究分支，请由研究人员决定是否补充其他资料或结束本轮研究。",
                    evidence_ids=tuple(),
                    next_modules=("manual_research",),
                    priority="normal",
                )
            )

        # Stable ordering and duplicate protection make output suitable for workflow state.
        deduped: dict[str, RouteSuggestion] = {}
        for suggestion in suggestions:
            deduped.setdefault(suggestion.route_id, suggestion)
        ordered = sorted(
            deduped.values(),
            key=lambda item: (0 if item.priority == "high" else 1, item.route_id),
        )
        return ResearchRoutePlan(suggestions=ordered)

    def confirm(
        self,
        plan: ResearchRoutePlan,
        selected_route_ids: list[str],
    ) -> ResearchRoutePlan:
        available = {item.route_id for item in plan.suggestions}
        selected = list(dict.fromkeys(selected_route_ids))
        unknown = [route_id for route_id in selected if route_id not in available]
        if unknown:
            raise ValueError(f"Unknown research route(s): {unknown}")
        return ResearchRoutePlan(
            suggestions=list(plan.suggestions),
            confirmed_route_ids=selected,
            human_confirmed=True,
        )

    def request_human_checkpoint(
        self,
        workflow: WorkflowService,
        project_id: str,
        plan: ResearchRoutePlan,
    ):
        return workflow.request_human_checkpoint(
            project_id,
            "research_route_review",
            "请确认下一步需要执行的研究分支。可多选，也可以暂不进入任何支线。",
            plan.to_dict(),
        )


def confirmed_modules(plan: ResearchRoutePlan) -> list[str]:
    """Resolve a human-confirmed route plan into a de-duplicated module queue."""
    if not plan.human_confirmed:
        raise ValueError("Research route plan must be human-confirmed before execution")
    selected = set(plan.confirmed_route_ids)
    modules: list[str] = []
    for suggestion in plan.suggestions:
        if suggestion.route_id not in selected:
            continue
        for module in suggestion.next_modules:
            if module not in modules:
                modules.append(module)
    return modules
