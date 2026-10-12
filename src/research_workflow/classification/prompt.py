from __future__ import annotations

import json

from research_workflow.enrichment.models import ASINProfile

from .models import ClassificationSchema


def build_classification_prompt(
    schema: ClassificationSchema,
    profiles: list[ASINProfile],
) -> str:
    schema.validate()
    labels = [
        {
            "key": label.key,
            "name": label.name,
            "description": label.description,
            "examples": list(label.examples),
        }
        for label in schema.labels
    ]
    products = [
        {
            "asin": p.asin,
            "title": p.title,
            "brand": p.brand,
            "bullets": p.bullets,
            "attributes": p.attributes,
        }
        for p in profiles
    ]
    return (
        "You are a product classification assistant. Classify only; do not make a "
        "Yes/No product decision. Use only the supplied evidence. If evidence is "
        "insufficient, use the unknown label. Return JSON only.\n\n"
        f"Dimension: {schema.dimension}\n"
        f"Unknown label: {schema.unknown_label}\n"
        f"Labels: {json.dumps(labels, ensure_ascii=False)}\n"
        f"Products: {json.dumps(products, ensure_ascii=False)}\n\n"
        "Return an array of objects with keys asin, label, confidence(high|medium|low), rationale."
    )
