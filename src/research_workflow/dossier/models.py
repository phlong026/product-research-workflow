from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research_workflow.analysis.models import MarketResearchReport
from research_workflow.classification.models import ClassificationBatch
from research_workflow.competition.models import CompetitionMetricsResult
from research_workflow.data.models import BSRRecord
from research_workflow.metrics.iteration import IterationResult
from research_workflow.metrics.models import MarketMetricsResult
from research_workflow.new_products.models import NewProductAnalysisResult
from research_workflow.router.models import ResearchRoutePlan


@dataclass(slots=True, frozen=True)
class SourceArtifact:
    artifact_id: str
    name: str
    kind: str
    source: str
    sha256: str | None = None
    note: str = ""
    # 相对 dossier 根目录/资料包的路径,供另一台电脑核验;缺省回退到 name。
    relative_path: str = ""


@dataclass(slots=True)
class ResearchDossierInput:
    project_id: str
    product: str
    marketplace: str
    market_metrics: MarketMetricsResult
    market_analysis: MarketResearchReport | None = None
    competition_metrics: CompetitionMetricsResult | None = None
    new_product_analysis: NewProductAnalysisResult | None = None
    route_plan: ResearchRoutePlan | None = None
    source_artifacts: list[SourceArtifact] = field(default_factory=list)
    data_quality: list[dict[str, object]] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 以下为综合 Excel 工作簿使用的可选字段;缺省时对应工作表写「本次运行未提供」。
    records: list[BSRRecord] | None = None
    classification: ClassificationBatch | None = None
    data_version: str | None = None
    data_hash: str | None = None
    approval_history: list[dict[str, object]] | None = None
    # 产品迭代进程(metrics/iteration.py);缺省 None 向后兼容,对应小节/工作表写「未提供」。
    iteration: IterationResult | None = None
    # 外部证据与预测(external_ingest.load_external 的输出,按 kind 分节);缺省 None 向后兼容,
    # 为 None 时报告不含「外部证据与预测」章节、工作簿不含外部_* 工作表。
    external: dict[str, Any] | None = None
