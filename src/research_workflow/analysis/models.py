from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class ResearchFinding:
    key: str
    title: str
    detail: str
    evidence_ids: tuple[str, ...]
    severity: str = "info"


@dataclass(slots=True)
class MarketResearchReport:
    summary: str
    findings: list[ResearchFinding]
    follow_up_questions: list[str] = field(default_factory=list)
    provider: str = "deterministic"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "findings": [asdict(row) for row in self.findings],
            "follow_up_questions": list(self.follow_up_questions),
            "provider": self.provider,
            "metadata": dict(self.metadata),
        }
