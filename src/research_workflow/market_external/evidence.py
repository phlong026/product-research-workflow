"""External market evidence ledger (R05).

Every market number coming from outside this system (Google Trends, research
reports, vendor reports) is recorded as an ``ExternalEvidence`` row that carries
its source, as-of date and file/url reference. Absent or unreachable sources are
recorded explicitly (``missing`` / ``inaccessible``) instead of being invented.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

RETRIEVED_FROM = ("file", "manual", "api")
STATUSES = ("present", "missing", "inaccessible")


@dataclass(frozen=True)
class ExternalEvidence:
    claim_id: str
    metric: str
    value: float | str | None
    unit: str
    period: str
    source_name: str
    source_url: str
    as_of: str
    retrieved_from: str
    status: str = "present"

    def __post_init__(self) -> None:
        if not self.claim_id.strip():
            raise ValueError("claim_id must not be empty")
        if self.retrieved_from not in RETRIEVED_FROM:
            raise ValueError(f"retrieved_from must be one of {RETRIEVED_FROM}: {self.retrieved_from!r}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}: {self.status!r}")
        if self.status == "present" and self.value is None:
            raise ValueError(f"{self.claim_id}: present evidence needs a value")
        if self.status != "present" and self.value is not None:
            raise ValueError(f"{self.claim_id}: {self.status} evidence must not carry a value")
        if self.as_of:
            try:
                date.fromisoformat(self.as_of)
            except ValueError as exc:
                raise ValueError(f"{self.claim_id}: as_of must be ISO date: {self.as_of!r}") from exc


class EvidenceLedger:
    def __init__(self) -> None:
        self._items: dict[str, ExternalEvidence] = {}

    def add(self, evidence: ExternalEvidence) -> ExternalEvidence:
        if evidence.claim_id in self._items:
            raise ValueError(f"duplicate claim_id: {evidence.claim_id}")
        self._items[evidence.claim_id] = evidence
        return evidence

    def get(self, claim_id: str) -> ExternalEvidence:
        try:
            return self._items[claim_id]
        except KeyError:
            raise KeyError(f"unknown evidence id: {claim_id}") from None

    def __contains__(self, claim_id: object) -> bool:
        return claim_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[ExternalEvidence]:
        return list(self._items.values())

    def missing(self) -> list[ExternalEvidence]:
        """Evidence that is not usable: absent or inaccessible sources."""
        return [e for e in self._items.values() if e.status != "present"]

    def require_sources(self) -> None:
        """Every present number must name its source, url/file reference and as-of date."""
        problems: list[str] = []
        for item in self._items.values():
            if item.status == "present":
                for field in ("source_name", "source_url", "as_of"):
                    if not getattr(item, field).strip():
                        problems.append(f"{item.claim_id}: missing {field}")
            elif item.status == "inaccessible" and not item.source_name.strip():
                problems.append(f"{item.claim_id}: inaccessible evidence must name the source")
        if problems:
            raise ValueError("evidence without source: " + "; ".join(problems))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": [asdict(e) for e in self._items.values()],
            "missing_claim_ids": [e.claim_id for e in self.missing()],
        }
