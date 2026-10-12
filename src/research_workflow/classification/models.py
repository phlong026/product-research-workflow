from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any


@dataclass(slots=True, frozen=True)
class ClassificationLabel:
    key: str
    name: str
    description: str = ""
    examples: tuple[str, ...] = ()


@dataclass(slots=True)
class ClassificationSchema:
    project_id: str
    dimension: str
    labels: list[ClassificationLabel]
    unknown_label: str = "unknown"
    notes: str = ""

    def validate(self) -> None:
        if not self.dimension.strip():
            raise ValueError("classification dimension is required")
        keys = [label.key for label in self.labels]
        if not keys:
            raise ValueError("at least one classification label is required")
        if len(keys) != len(set(keys)):
            raise ValueError("classification label keys must be unique")
        if self.unknown_label in keys:
            raise ValueError("unknown_label must not duplicate a regular label")

    @property
    def allowed_labels(self) -> set[str]:
        return {label.key for label in self.labels} | {self.unknown_label}

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "dimension": self.dimension,
            "labels": [asdict(label) for label in self.labels],
            "unknown_label": self.unknown_label,
            "notes": self.notes,
        }

    @property
    def schema_hash(self) -> str:
        raw = json.dumps(
            self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(raw).hexdigest()[:16]


@dataclass(slots=True)
class ProductClassification:
    asin: str
    label: str
    confidence: str = "medium"
    rationale: str = ""
    source: str = "llm"


@dataclass(slots=True)
class ClassificationBatch:
    project_id: str
    schema_hash: str
    version: str
    rows: dict[str, ProductClassification]
    approved: bool = False
    human_edits: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "schema_hash": self.schema_hash,
            "version": self.version,
            "rows": {asin: asdict(row) for asin, row in self.rows.items()},
            "approved": self.approved,
            "human_edits": dict(self.human_edits),
        }
