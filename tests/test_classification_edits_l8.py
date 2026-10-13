from __future__ import annotations

import asyncio

from research_workflow.classification import (
    ClassificationLabel,
    ClassificationSchema,
    ClassificationService,
    RuleClassificationProvider,
)
from research_workflow.enrichment import ASINProfile


def ice_schema():
    return ClassificationSchema(
        project_id="p1",
        dimension="ice_type",
        labels=[
            ClassificationLabel("bullet", "子弹冰", "bullet-shaped ice", ("bullet ice",)),
            ClassificationLabel("nugget", "咀嚼冰", "chewable nugget/pebble ice", ("nugget ice",)),
            ClassificationLabel("cube", "方块冰", "cube/square ice", ("cube ice",)),
        ],
    )


def test_apply_human_edits_merges_with_previous_edits():
    schema = ice_schema()
    service = ClassificationService(RuleClassificationProvider({}))
    batch = asyncio.run(
        service.classify(
            schema, [ASINProfile(asin="A1", title="Ice Maker"), ASINProfile(asin="A2", title="Ice Maker")]
        )
    )
    first = service.apply_human_edits(schema, batch, {"A1": "bullet"})
    second = service.apply_human_edits(schema, first, {"a2": "cube"})
    assert second.human_edits == {"A1": "bullet", "A2": "cube"}
    assert second.rows["A1"].label == "bullet" and second.rows["A1"].source == "human"
    # 同一 ASIN 再次修改以最新为准
    third = service.apply_human_edits(schema, second, {"A1": "nugget"})
    assert third.human_edits == {"A1": "nugget", "A2": "cube"}
    # 不修改入参
    assert first.human_edits == {"A1": "bullet"}
