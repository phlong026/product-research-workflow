from __future__ import annotations

import asyncio

import pytest

from research_workflow.classification import (
    ClassificationLabel,
    ClassificationSchema,
    ClassificationService,
    RuleClassificationProvider,
    build_classification_prompt,
)
from research_workflow.enrichment import ASINProfile
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


def ice_schema(project_id="p1"):
    return ClassificationSchema(
        project_id=project_id,
        dimension="ice_type",
        labels=[
            ClassificationLabel("bullet", "子弹冰", "bullet-shaped ice", ("bullet ice",)),
            ClassificationLabel("nugget", "咀嚼冰", "chewable nugget/pebble ice", ("nugget ice",)),
            ClassificationLabel("cube", "方块冰", "cube/square ice", ("cube ice",)),
        ],
    )


def test_schema_hash_is_stable_and_prompt_forbids_yes_no_decision():
    a = ice_schema()
    b = ice_schema()
    assert a.schema_hash == b.schema_hash
    prompt = build_classification_prompt(
        a, [ASINProfile(asin="A1", title="Nugget Ice Maker", bullets=["Chewable ice"])]
    )
    assert "do not make a Yes/No product decision" in prompt
    assert '"key": "nugget"' in prompt


def test_rule_classifier_returns_unknown_for_ambiguous_and_human_can_correct():
    schema = ice_schema()
    provider = RuleClassificationProvider(
        {"bullet": ["bullet"], "nugget": ["nugget", "pebble"], "cube": ["cube"]}
    )
    service = ClassificationService(provider)
    batch = asyncio.run(
        service.classify(
            schema,
            [
                ASINProfile(asin="A1", title="Nugget Ice Maker"),
                ASINProfile(asin="A2", title="Ice Maker"),
            ],
        )
    )
    assert batch.rows["A1"].label == "nugget"
    assert batch.rows["A2"].label == "unknown"
    assert batch.approved is False

    approved = service.apply_human_edits(schema, batch, {"A2": "bullet"})
    assert approved.approved is True
    assert approved.rows["A2"].label == "bullet"
    assert approved.rows["A2"].source == "human"
    assert approved.version != batch.version


def test_invalid_human_edit_is_rejected():
    schema = ice_schema()
    service = ClassificationService(RuleClassificationProvider({}))
    batch = asyncio.run(service.classify(schema, [ASINProfile(asin="A1", title="Ice Maker")]))
    with pytest.raises(ValueError):
        service.apply_human_edits(schema, batch, {"A1": "yes"})


def test_classification_review_is_a_real_human_checkpoint():
    store = SQLiteStore()
    workflow = WorkflowService(store)
    project = workflow.create_project("ice maker", "US")
    schema = ice_schema(project.id)
    service = ClassificationService(RuleClassificationProvider({"nugget": ["nugget"]}))
    batch = asyncio.run(
        service.classify(schema, [ASINProfile(asin="A1", title="Nugget Ice Maker")])
    )
    cp = service.request_review_checkpoint(workflow, schema, batch)
    snapshot = workflow.snapshot(project.id)
    assert cp.step_key == "classification_review"
    assert snapshot["project"]["status"] == "paused"
    assert snapshot["state"]["pending_checkpoint_id"] == cp.id


def test_rule_classifier_multi_match_goes_to_review_but_zero_match_is_plain_unknown():
    schema = ice_schema()
    provider = RuleClassificationProvider({"bullet": ["bullet"], "nugget": ["nugget"], "cube": ["cube"]})
    service = ClassificationService(provider)
    batch = asyncio.run(
        service.classify(
            schema,
            [
                ASINProfile(asin="M1", title="Nugget and Cube Ice Maker"),
                ASINProfile(asin="Z1", title="Ice Maker"),
                ASINProfile(asin="S1", title="Bullet Ice Maker"),
            ],
        )
    )
    multi, zero, single = batch.rows["M1"], batch.rows["Z1"], batch.rows["S1"]
    assert multi.label == "unknown" and multi.needs_review is True
    assert multi.review_reason == "多个标签同时命中: cube,nugget"  # 标签按名称排序,输出稳定
    # 零命中是显式"未归类"类别:保持 unknown,不进待人工
    assert zero.label == "unknown" and zero.needs_review is False and zero.review_reason == ""
    assert single.label == "bullet" and single.needs_review is False

