from __future__ import annotations

from dataclasses import dataclass, field

from research_workflow.analysis.models import MarketResearchReport
from research_workflow.competition.models import CompetitionMetricsResult
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
    metadata: dict[str, str] = field(default_factory=dict)
