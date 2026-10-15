"""把一个已完成项目整理成可归档的知识库条目(纯函数,不读盘;由 CLI 收集输入)。"""
from __future__ import annotations

from typing import Any

from research_workflow.reconcile import flatten_metrics

SCHEMA_VERSION = 1


def build_payload(
    *, project: dict[str, Any], classification_envelope: dict[str, Any], market: dict[str, Any] | None,
    competition: dict[str, Any] | None, new_products: dict[str, Any] | None, context: dict[str, Any],
    approvals: list[dict[str, Any]], external_index: list[dict[str, Any]], deep_dive: dict[str, Any] | None,
    sources: dict[str, str | None], human_notes: list[str],
) -> dict[str, Any]:
    schema, batch = classification_envelope["schema"], classification_envelope["batch"]
    edited = set(batch.get("human_edits", {}))
    np_summary = (new_products or {}).get("summary") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "classification": {
            "dimension": schema["dimension"], "schema_hash": batch["schema_hash"], "version": batch["version"],
            "approved": bool(batch.get("approved")), "provider": classification_envelope.get("provider", "rules"),
            "unknown_label": schema.get("unknown_label", "unknown"),
            "labels": [{"key": label["key"], "name": label["name"], "description": label.get("description", ""),
                        "examples": list(label.get("examples", []))} for label in schema["labels"]],
            "asin_labels": [{"asin": asin, "label": row["label"], "confidence": row.get("confidence"), "human_edited": asin in edited}
                            for asin, row in sorted(batch["rows"].items())],
        },
        "metrics": flatten_metrics(market, competition, new_products),
        "thresholds": {
            "promotion_waist_sales_threshold": np_summary.get("promotion_waist_sales_threshold") if new_products else None,
            "retention_sales_threshold": np_summary.get("retention_sales_threshold") if new_products else None,
            "new_products_skipped_reason": context.get("new_products_skipped_reason"),
        },
        "approvals": approvals,
        "external": external_index,
        "deep_dive": None if deep_dive is None else {
            "modules": deep_dive["report"]["requested_modules"],
            "missing_slots": [f"{s['title']}/{slot['label']}" for s in deep_dive["report"]["sections"]
                              for slot in s["slots"] if slot["status"] == "missing"],
            "unsupported": [item["module"] for item in deep_dive["report"]["unsupported"]],
        },
        "sources": sources,
        "human_notes": human_notes,
    }
