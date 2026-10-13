from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class EvidenceRecord:
    evidence_id: str
    slot: str
    claim: str
    value: float | str | None
    unit: str | None
    period: str | None
    geography: str | None
    scope: str | None
    source_name: str
    source_url: str | None
    source_file: str | None
    source_sha256: str | None
    retrieved_at: str
    added_by: str
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class ModuleFinding:
    """深挖结论：必须引用已入账的证据；只陈述研究发现，不含 Yes/No 决策。"""

    finding_id: str
    module: str
    text: str
    evidence_ids: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class SlotStatus:
    slot: str
    module: str
    title: str
    evidence_count: int
    status: str  # covered | missing
    provider_hint: str
