#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research_workflow.analysis import MarketResearchService
from research_workflow.classification import (
    ClassificationLabel,
    ClassificationSchema,
    ClassificationService,
    RuleClassificationProvider,
)
from research_workflow.competition import build_competition_metrics
from research_workflow.data import MonthlyBSRConfig, build_monthly_top
from research_workflow.dossier import ResearchDossierGenerator, ResearchDossierInput, SourceArtifact
from research_workflow.enrichment import ASINEnrichmentService
from research_workflow.metrics import build_market_metrics
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products
from research_workflow.router import ResearchRouter


class DemoProductProvider:
    name = "offline-demo"

    def __init__(self, payloads):
        self.payloads = payloads

    async def get_product_detail(self, asin: str, marketplace: str):
        return self.payloads.get(asin)


async def run(output: Path) -> Path:
    raw_by_month = {
        "2025-01": [
            {"ASIN": "N1", "月销量": 20, "品牌": "NBrand", "小类BSR": 1, "卖家所属地": "CN"},
            {"ASIN": "B1", "月销量": 80, "品牌": "BBrand", "小类BSR": 2, "卖家所属地": "US"},
        ],
        "2026-01": [
            {"ASIN": "N1", "月销量": 25, "品牌": "NBrand", "小类BSR": 1, "卖家所属地": "CN"},
            {"ASIN": "B1", "月销量": 55, "品牌": "BBrand", "小类BSR": 2, "卖家所属地": "US"},
        ],
    }

    records = []
    for month, rows in raw_by_month.items():
        records.extend(build_monthly_top(rows, month, MonthlyBSRConfig(top_n=100)).records)

    provider = DemoProductProvider(
        {
            "N1": {"asin": "N1", "title": "Nugget Ice Maker", "brand": "NBrand", "features": ["Chewable nugget ice"]},
            "B1": {"asin": "B1", "title": "Bullet Ice Maker", "brand": "BBrand", "features": ["Bullet-shaped ice"]},
        }
    )
    enriched = await ASINEnrichmentService(provider).enrich([r.asin for r in records], "US")

    schema = ClassificationSchema(
        project_id="offline-demo",
        dimension="ice_type",
        labels=[
            ClassificationLabel("nugget", "咀嚼冰"),
            ClassificationLabel("bullet", "子弹冰"),
        ],
    )
    classifier = ClassificationService(
        RuleClassificationProvider({"nugget": ["nugget"], "bullet": ["bullet"]})
    )
    classification = await classifier.classify(schema, list(enriched.profiles.values()))
    classification = classifier.apply_human_edits(schema, classification, {})

    market = build_market_metrics(
        records,
        classification.rows,
        decomposition_pairs=[("2025-01", "2026-01")],
    )
    market_analysis = await MarketResearchService().analyze(market)
    competition = build_competition_metrics(
        records,
        rank_cutoffs=(1, 2),
        head_cutoff=1,
        broad_cutoff=2,
    )
    new_products = analyze_new_products(
        [
            NewProductSnapshot("X1", "2026-01", 300, account_type="VC"),
            NewProductSnapshot("X1", "2026-02", 300, account_type="VC"),
            NewProductSnapshot("X1", "2026-03", 300, account_type="VC"),
        ],
        NewProductConfig(250, 3, 3),
    )
    router = ResearchRouter()
    route_plan = router.suggest(market, competition, new_products)
    route_plan = router.confirm(
        route_plan,
        [route.route_id for route in route_plan.suggestions if route.priority == "high"],
    )

    dossier = ResearchDossierInput(
        project_id="offline-demo",
        product="Ice Maker",
        marketplace="US",
        market_metrics=market,
        market_analysis=market_analysis,
        competition_metrics=competition,
        new_product_analysis=new_products,
        route_plan=route_plan,
        source_artifacts=[
            SourceArtifact("demo", "built-in synthetic fixture", "fixture", "offline")
        ],
    )
    return ResearchDossierGenerator().generate(dossier, output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="runtime/offline-demo")
    args = parser.parse_args()
    path = asyncio.run(run(Path(args.output)))
    print(path)


if __name__ == "__main__":
    main()
