from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExpectedMetric:
    value: float | int
    tolerance: float = 0.0
    unit: str | None = None
    note: str | None = None

    def matches(self, actual: float | int) -> bool:
        return abs(float(actual) - float(self.value)) <= self.tolerance


@dataclass(frozen=True)
class GoldenCaseBaseline:
    case_id: str
    product: str
    marketplace: str
    final_decision_owner: str
    metrics: dict[str, ExpectedMetric]
    source_notes: list[str]


def load_baseline(path: str | Path) -> GoldenCaseBaseline:
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    metrics = {
        key: ExpectedMetric(
            value=value["value"],
            tolerance=value.get("tolerance", 0.0),
            unit=value.get("unit"),
            note=value.get("note"),
        )
        for key, value in payload["metrics"].items()
    }
    return GoldenCaseBaseline(
        case_id=payload["case_id"],
        product=payload["product"],
        marketplace=payload["marketplace"],
        final_decision_owner=payload["final_decision_owner"],
        metrics=metrics,
        source_notes=payload.get("source_notes", []),
    )
