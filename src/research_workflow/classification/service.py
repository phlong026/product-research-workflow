from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from typing import Any, Protocol

from research_workflow.enrichment.models import ASINProfile
from research_workflow.workflow.service import WorkflowService

from .models import (
    ClassificationBatch,
    ClassificationSchema,
    ProductClassification,
)
from .prompt import build_classification_prompt


class ClassificationProvider(Protocol):
    name: str

    async def classify(
        self,
        schema: ClassificationSchema,
        profiles: list[ASINProfile],
    ) -> list[dict[str, Any]]: ...


class CallbackClassificationProvider:
    """Runtime-neutral adapter for any structured-output LLM caller.

    The callback receives the rendered prompt and must return decoded JSON rows.
    This keeps the core package independent from a specific LLM vendor SDK.
    """

    def __init__(self, callback, *, name: str = "llm") -> None:
        self.callback = callback
        self.name = name

    async def classify(self, schema, profiles):
        prompt = build_classification_prompt(schema, profiles)
        result = self.callback(prompt)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, list):
            raise ValueError("classification provider must return a list")
        return result


class RuleClassificationProvider:
    """Deterministic fallback useful for tests and simple first-pass rules."""

    name = "rules"

    def __init__(self, keywords_by_label: dict[str, list[str]]) -> None:
        self.keywords_by_label = {
            key: [word.lower() for word in words] for key, words in keywords_by_label.items()
        }

    async def classify(self, schema, profiles):
        rows = []
        for profile in profiles:
            text = " ".join([profile.title, *profile.bullets]).lower()
            matches = [
                label
                for label, words in self.keywords_by_label.items()
                if any(word in text for word in words)
            ]
            label = matches[0] if len(matches) == 1 else schema.unknown_label
            rows.append(
                {
                    "asin": profile.asin,
                    "label": label,
                    "confidence": "high" if len(matches) == 1 else "low",
                    "rationale": "keyword rule" if len(matches) == 1 else "ambiguous or no match",
                }
            )
        return rows


class ClassificationService:
    def __init__(self, provider: ClassificationProvider) -> None:
        self.provider = provider

    async def classify(
        self,
        schema: ClassificationSchema,
        profiles: list[ASINProfile],
    ) -> ClassificationBatch:
        schema.validate()
        unique = {profile.asin.upper(): profile for profile in profiles}
        raw_rows = await self.provider.classify(schema, list(unique.values()))

        rows: dict[str, ProductClassification] = {}
        for raw in raw_rows:
            asin = str(raw.get("asin", "")).strip().upper()
            if asin not in unique:
                continue
            label = str(raw.get("label", schema.unknown_label)).strip()
            needs_review = bool(raw.get("needs_review", False))
            review_reason = str(raw.get("review_reason", "")).strip()
            if label not in schema.allowed_labels:
                # Never invent a class: an out-of-schema label goes to a human.
                label = schema.unknown_label
                needs_review = True
                review_reason = review_reason or "label not in schema"
            confidence = str(raw.get("confidence", "medium")).lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            rows[asin] = ProductClassification(
                asin=asin,
                label=label,
                confidence=confidence,
                rationale=str(raw.get("rationale", "")).strip(),
                source=self.provider.name,
                evidence=str(raw.get("evidence", "")).strip(),
                needs_review=needs_review,
                review_reason=review_reason,
            )

        # Every requested ASIN must be represented so nothing silently disappears.
        for asin in unique:
            rows.setdefault(
                asin,
                ProductClassification(
                    asin=asin,
                    label=schema.unknown_label,
                    confidence="low",
                    rationale="provider returned no classification",
                    source=self.provider.name,
                    needs_review=True,
                    review_reason="provider returned no classification",
                ),
            )

        version = _version(schema, rows)
        return ClassificationBatch(
            project_id=schema.project_id,
            schema_hash=schema.schema_hash,
            version=version,
            rows=rows,
        )

    def apply_human_edits(
        self,
        schema: ClassificationSchema,
        batch: ClassificationBatch,
        edits: dict[str, str],
    ) -> ClassificationBatch:
        if batch.project_id != schema.project_id or batch.schema_hash != schema.schema_hash:
            raise ValueError("classification batch does not belong to this schema")
        rows = {asin: replace(row) for asin, row in batch.rows.items()}
        normalized_edits: dict[str, str] = {}
        for raw_asin, label in edits.items():
            asin = raw_asin.strip().upper()
            if asin not in rows:
                raise KeyError(f"Unknown ASIN in classification edits: {asin}")
            if label not in schema.allowed_labels:
                raise ValueError(f"Invalid classification label: {label}")
            rows[asin] = ProductClassification(
                asin=asin,
                label=label,
                confidence="high",
                rationale="human corrected",
                source="human",
            )
            normalized_edits[asin] = label

        return ClassificationBatch(
            project_id=batch.project_id,
            schema_hash=batch.schema_hash,
            version=_version(schema, rows),
            rows=rows,
            approved=True,
            human_edits=normalized_edits,
        )

    def request_review_checkpoint(
        self,
        workflow: WorkflowService,
        schema: ClassificationSchema,
        batch: ClassificationBatch,
    ):
        return workflow.request_human_checkpoint(
            schema.project_id,
            "classification_review",
            "请检查 AI 产品分类；可修改分类后确认继续。",
            {
                "schema": schema.canonical_payload(),
                "classification": batch.to_dict(),
            },
        )


def _version(schema: ClassificationSchema, rows: dict[str, ProductClassification]) -> str:
    payload = {
        "schema_hash": schema.schema_hash,
        "rows": {
            asin: _row_fingerprint(row) for asin, row in sorted(rows.items())
        },
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    return f"cls-{digest}"


def _row_fingerprint(row: ProductClassification) -> dict[str, Any]:
    fingerprint: dict[str, Any] = {
        "label": row.label,
        "confidence": row.confidence,
        "source": row.source,
    }
    # Only present when set, so versions of batches without review flags stay unchanged.
    # evidence/rationale are descriptive text and deliberately excluded from the version.
    if row.needs_review:
        fingerprint["needs_review"] = True
    return fingerprint
