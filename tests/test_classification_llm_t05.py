from __future__ import annotations

import asyncio
import json

import pytest

from research_workflow.classification import (
    NEEDS_REVIEW,
    ClassificationLabel,
    ClassificationSchema,
    ClassificationService,
    LLMClassificationProvider,
    RuleClassificationProvider,
    evaluate,
)
from research_workflow.enrichment import ASINProfile

# Synthetic products: fictional "widget" shapes, fake ASINs.
SCHEMA = ClassificationSchema(
    project_id="p1",
    dimension="widget_shape",
    labels=[
        ClassificationLabel("round", "圆形", "round widget"),
        ClassificationLabel("square", "方形", "square widget"),
        ClassificationLabel("hex", "六角形", "hexagonal widget"),
        ClassificationLabel("oval", "椭圆形", "oval widget"),
    ],
)

PROFILES = [
    ASINProfile(asin="T1", title="Round Widget Pro", bullets=["Perfectly round design"]),
    ASINProfile(asin="T2", title="Square Widget Mini", bullets=["Compact square body"]),
    ASINProfile(asin="T3", title="Hex Widget", bullets=["Six-sided hexagonal frame"]),
    ASINProfile(asin="T4", title="Oval Widget", bullets=["Smooth oval shape"]),
]
GOLD = {"T1": "round", "T2": "square", "T3": "hex", "T4": "oval"}


def row(asin, label, evidence, confidence="high", **extra):
    return {"asin": asin, "label": label, "confidence": confidence, "evidence": evidence, **extra}


class FakeComplete:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


def classify(complete, profiles=PROFILES, **kwargs):
    provider = LLMClassificationProvider(complete, **kwargs)
    return asyncio.run(ClassificationService(provider).classify(SCHEMA, profiles))


def test_valid_output_is_classified_with_evidence_and_prompt_has_title_and_bullets():
    complete = FakeComplete(
        [
            row("T1", "round", "Perfectly round design"),
            row("T2", "square", "square body"),
            row("T3", "hex", "HEXAGONAL   frame", confidence=0.9),
            row("T4", "oval", "Oval Widget"),
        ]
    )
    batch = classify(complete)
    assert {a: r.label for a, r in batch.rows.items()} == GOLD
    assert not any(r.needs_review for r in batch.rows.values())
    assert batch.rows["T3"].confidence == "high"
    assert batch.rows["T1"].evidence == "Perfectly round design"
    assert batch.rows["T1"].source == "llm"
    prompt = complete.prompts[0]
    assert "Round Widget Pro" in prompt and "Perfectly round design" in prompt
    assert "do not make a Yes/No product decision" in prompt


def test_invalid_missing_low_confidence_ambiguous_and_ungrounded_go_to_human_review():
    complete = FakeComplete(
        [
            row("T1", "triangle", "Perfectly round design"),  # label not in schema
            row("T2", "square", "Compact square body", confidence="low"),  # below threshold
            row("T3", "hex", "Six-sided hexagonal frame", ambiguous=True),  # ambiguous
            row("T4", "oval", "made of titanium"),  # evidence not in source
        ]
    )
    batch = classify(complete)
    for asin in GOLD:
        r = batch.rows[asin]
        assert r.needs_review is True and r.label == "unknown", asin
        assert r.review_reason
    assert "label not in schema" in batch.rows["T1"].review_reason
    assert "below medium" in batch.rows["T2"].review_reason
    assert "'square'" in batch.rows["T2"].review_reason  # suggestion kept for the human only
    assert "ambiguous" in batch.rows["T3"].review_reason
    assert "title/bullets" in batch.rows["T4"].review_reason
    assert batch.rows["T4"].evidence == ""


def test_missing_row_and_missing_label_are_never_invented():
    batch = classify(
        FakeComplete([{"asin": "T1", "confidence": "high", "evidence": "round"}, row("T2", "square", "square")])
    )
    assert batch.rows["T1"].needs_review and batch.rows["T1"].label == "unknown"
    assert batch.rows["T3"].needs_review and "no row" in batch.rows["T3"].review_reason
    assert batch.rows["T2"].label == "square" and not batch.rows["T2"].needs_review


def test_model_may_answer_unknown_and_that_is_review_not_a_class():
    batch = classify(FakeComplete([row("T1", "unknown", "Round Widget Pro")]), PROFILES[:1])
    assert batch.rows["T1"].needs_review is True


def test_garbage_json_is_retried_then_whole_chunk_goes_to_review():
    complete = FakeComplete("not json")
    batch = classify(complete, attempts=3)
    assert len(complete.prompts) == 3
    assert all(r.needs_review for r in batch.rows.values())
    assert "failed after 3 attempts" in batch.rows["T1"].review_reason


def test_retry_recovers_after_transient_failure_and_fenced_json_is_accepted():
    good = "```json\n" + json.dumps([row("T1", "round", "round")]) + "\n```"
    complete = FakeComplete(RuntimeError("timeout"), good)
    batch = classify(complete, PROFILES[:1], attempts=2)
    assert len(complete.prompts) == 2
    assert batch.rows["T1"].label == "round" and not batch.rows["T1"].needs_review


def test_batches_are_chunked():
    complete = FakeComplete([])
    classify(complete, chunk_size=3, attempts=1)
    assert len(complete.prompts) == 2  # 4 products, chunk of 3
    assert "Oval Widget" in complete.prompts[1] and "Oval Widget" not in complete.prompts[0]


def test_min_confidence_is_configurable():
    complete = FakeComplete([row("T1", "round", "round", confidence="medium")])
    assert classify(complete, PROFILES[:1], min_confidence="high").rows["T1"].needs_review
    assert not classify(complete, PROFILES[:1], min_confidence="low").rows["T1"].needs_review


def test_version_is_content_hash_frozen_and_review_flag_changes_it():
    ok = [row("T1", "round", "round")]
    a = classify(FakeComplete(ok), PROFILES[:1])
    b = classify(FakeComplete(ok), PROFILES[:1])
    assert a.version == b.version and a.schema_hash == SCHEMA.schema_hash
    pending = classify(FakeComplete([row("T1", "round", "round", ambiguous=True)]), PROFILES[:1])
    assert pending.version != a.version
    # evidence wording is descriptive only and does not change the frozen version
    reworded = classify(FakeComplete([row("T1", "round", "Round Widget Pro")]), PROFILES[:1])
    assert reworded.version == a.version


def test_rule_provider_remains_a_fallback_for_review_rows():
    llm = classify(FakeComplete([row("T1", "triangle", "round")]), PROFILES[:1])
    assert llm.rows["T1"].needs_review
    fallback = asyncio.run(
        ClassificationService(RuleClassificationProvider({"round": ["round"]})).classify(
            SCHEMA, PROFILES[:1]
        )
    )
    assert fallback.rows["T1"].label == "round"


# ---- guardrail: classification never carries a business Yes/No -------------


def test_business_decision_content_is_rejected_to_review():
    batch = classify(
        FakeComplete(
            [
                row("T1", "round", "round", recommendation="Yes, enter this market"),
                row("T2", "square", "square", verdict="go"),
                row("T3", "hex", "hexagonal", comment="Recommend 建议进入"),
            ]
        ),
        PROFILES[:3],
    )
    assert all(r.needs_review for r in batch.rows.values())
    assert all("business-decision" in r.review_reason for r in batch.rows.values())
    assert "recommendation" not in json.dumps(batch.to_dict())


def test_yes_no_is_not_a_valid_label():
    batch = classify(FakeComplete([row("T1", "yes", "round")]), PROFILES[:1])
    assert batch.rows["T1"].needs_review and batch.rows["T1"].label == "unknown"


def test_evidence_is_descriptive_and_never_drives_label_or_conclusion():
    # Evidence mentions another class and decision-sounding words, but it is verbatim
    # source text: the label comes only from the label field.
    profile = ASINProfile(
        asin="T1",
        title="Round Widget",
        bullets=["Not square, not hex. Yes, it is round", "No assembly needed"],
    )
    batch = classify(
        FakeComplete([row("T1", "round", ["Not square, not hex", "No assembly needed"])]), [profile]
    )
    r = batch.rows["T1"]
    assert r.label == "round" and not r.needs_review
    assert "square" in r.evidence  # kept as quoted text...
    payload = batch.to_dict()["rows"]["T1"]
    assert set(payload) == {
        "asin", "label", "confidence", "rationale", "source",
        "evidence", "needs_review", "review_reason",
    }  # ...and no decision/recommendation field exists on the output
    assert payload["label"] in SCHEMA.allowed_labels


# ---- evaluation ----------------------------------------------------------


def test_evaluate_accuracy_confusion_and_review_rate():
    complete = FakeComplete(
        [
            row("T1", "round", "round"),  # correct
            row("T2", "round", "square"),  # wrong (square predicted round)
            row("T3", "hex", "hexagonal", ambiguous=True),  # pending
            # T4 missing -> pending
        ]
    )
    batch = classify(complete)
    report = evaluate(batch, GOLD)
    assert (report.total, report.answered, report.correct, report.needs_review) == (4, 2, 1, 2)
    assert report.accuracy == 0.5
    assert report.overall_accuracy == 0.25
    assert report.review_rate == 0.5
    assert report.confusion == {
        "hex": {NEEDS_REVIEW: 1},
        "oval": {NEEDS_REVIEW: 1},
        "round": {"round": 1},
        "square": {"round": 1},
    }


def test_evaluate_perfect_and_all_pending_edge_cases():
    perfect = evaluate({a: label for a, label in GOLD.items()}, GOLD)
    assert perfect.accuracy == 1.0 and perfect.review_rate == 0.0
    empty = evaluate({}, GOLD)
    assert empty.accuracy is None and empty.review_rate == 1.0
    assert empty.missing_predictions == ("T1", "T2", "T3", "T4")
    assert evaluate({}, {}).overall_accuracy is None


def test_constructor_validates_config():
    with pytest.raises(ValueError):
        LLMClassificationProvider(FakeComplete([]), min_confidence="certain")
    with pytest.raises(ValueError):
        LLMClassificationProvider(FakeComplete([]), chunk_size=0)


def test_decision_patterns_stay_in_sync_with_market_analysis_guard():
    from research_workflow.analysis.market import FORBIDDEN_DECISION_PATTERNS as market
    from research_workflow.classification.llm_provider import FORBIDDEN_DECISION_PATTERNS as local

    assert local == market
