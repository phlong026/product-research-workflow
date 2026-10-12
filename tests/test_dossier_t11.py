from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from research_workflow.analysis import MarketResearchService
from research_workflow.competition import build_competition_metrics
from research_workflow.data.models import BSRRecord
from research_workflow.dossier import (
    ResearchDossierGenerator,
    ResearchDossierInput,
    SourceArtifact,
)
from research_workflow.metrics import build_market_metrics
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products
from research_workflow.router import ResearchRouter


def sample_input(tmp_path):
    records = [
        BSRRecord("N", "2025-01", 20, brand="N", node_rank=1, seller_region="CN"),
        BSRRecord("B", "2025-01", 80, brand="B", node_rank=2, seller_region="US"),
        BSRRecord("N", "2026-01", 25, brand="N", node_rank=1, seller_region="CN"),
        BSRRecord("B", "2026-01", 55, brand="B", node_rank=2, seller_region="US"),
    ]
    market = build_market_metrics(
        records,
        {"N": "nugget", "B": "bullet"},
        decomposition_pairs=[("2025-01", "2026-01")],
    )
    competition = build_competition_metrics(
        records, rank_cutoffs=(1, 2), head_cutoff=1, broad_cutoff=2
    )
    new_products = analyze_new_products(
        [
            NewProductSnapshot("X", "2026-01", 300, account_type="VC"),
            NewProductSnapshot("X", "2026-02", 300, account_type="VC"),
            NewProductSnapshot("X", "2026-03", 300, account_type="VC"),
        ],
        NewProductConfig(250, 3, 3),
    )
    import asyncio

    analysis = asyncio.run(MarketResearchService().analyze(market))
    route = ResearchRouter().suggest(market, competition, new_products)
    return ResearchDossierInput(
        project_id="p1",
        product="Ice Maker",
        marketplace="US",
        market_metrics=market,
        market_analysis=analysis,
        competition_metrics=competition,
        new_product_analysis=new_products,
        route_plan=route,
        source_artifacts=[
            SourceArtifact(
                "src-1",
                "seller_sprite.xlsx",
                "xlsx",
                "SellerSprite",
                sha256(b"fake").hexdigest(),
                "test fixture",
            )
        ],
    )


def test_dossier_writes_portable_research_bundle(tmp_path):
    dossier = sample_input(tmp_path)
    root = ResearchDossierGenerator().generate(dossier, tmp_path / "dossier")
    expected = {
        "data.json",
        "source_index.json",
        "report.md",
        "dashboard.html",
        "manifest.json",
        "csv/market.csv",
        "csv/segments.csv",
        "csv/seller_regions.csv",
        "csv/concentration.csv",
        "csv/rank_layers.csv",
        "csv/new_product_outcomes.csv",
    }
    actual = {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    }
    assert expected <= actual


def test_manifest_matches_generated_file_bytes(tmp_path):
    root = ResearchDossierGenerator().generate(sample_input(tmp_path), tmp_path / "dossier")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]
    for entry in manifest["files"]:
        path = root / entry["path"]
        assert path.stat().st_size == entry["size"]
        assert sha256(path.read_bytes()).hexdigest() == entry["sha256"]


def test_dossier_marks_human_as_decision_owner_without_auto_decision(tmp_path):
    root = ResearchDossierGenerator().generate(sample_input(tmp_path), tmp_path / "dossier")
    payload = json.loads((root / "data.json").read_text(encoding="utf-8"))
    assert payload["project"]["decision_owner"] == "human"
    assert "decision" not in payload["project"]
    report = (root / "report.md").read_text(encoding="utf-8")
    assert "最终商业判断：由人工完成" in report
    assert "建议进入" not in report
    assert "不建议进入" not in report


def test_dashboard_contains_real_chart_payload_not_placeholder(tmp_path):
    root = ResearchDossierGenerator().generate(sample_input(tmp_path), tmp_path / "dossier")
    html = (root / "dashboard.html").read_text(encoding="utf-8")
    assert "echarts.init" in html
    assert '"2026-01"' in html
    assert '"nugget"' in html
