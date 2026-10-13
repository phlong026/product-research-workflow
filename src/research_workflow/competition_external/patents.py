"""R21 patent evidence ledger. Records facts about searches only; never a legal conclusion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger import (
    EvidenceRef,
    clean_text,
    evidence_from_row,
    guarded,
    read_rows,
    require_text,
)

PATENT_DISCLAIMER = "仅为检索记录,不构成法律意见;未发现不等于不存在。"
SEARCH_STATUSES = ("searched_found", "searched_none", "not_searched")
_STATUS_LABELS = {
    "searched_found": "已检索-发现记录",
    "searched_none": "已检索-范围内未发现",
    "not_searched": "未检索",
}


@dataclass(frozen=True, slots=True)
class PatentRecord:
    patent_no: str
    title: str
    evidence: EvidenceRef
    related_asin: str | None = None
    related_product: str | None = None
    filed_date: str | None = None
    publication_date: str | None = None
    search_query: str | None = None

    def __post_init__(self) -> None:
        require_text(self.patent_no, "patent_no")
        require_text(self.title, "title")
        if not (clean_text(self.related_asin) or clean_text(self.related_product)):
            raise ValueError("PatentRecord requires related_asin or related_product")

    def to_dict(self) -> dict[str, Any]:
        return {
            "patent_no": self.patent_no,
            "title": self.title,
            "related_asin": self.related_asin,
            "related_product": self.related_product,
            "filed_date": self.filed_date,
            "publication_date": self.publication_date,
            "search_query": self.search_query,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PatentSearchLog:
    """One search attempt. status distinguishes 'not searched' from 'searched, none found'."""

    query: str
    database: str
    scope: str
    status: str
    searched_at: str | None = None
    records: tuple[PatentRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_text(self.query, "query")
        require_text(self.database, "database")
        require_text(self.scope, "scope")
        if self.status not in SEARCH_STATUSES:
            raise ValueError(f"status must be one of {SEARCH_STATUSES}, got {self.status!r}")
        if self.status != "not_searched" and not clean_text(self.searched_at):
            raise ValueError("searched_at is required when the search was performed")
        if self.status == "searched_found" and not self.records:
            raise ValueError("searched_found requires at least one PatentRecord")
        if self.status != "searched_found" and self.records:
            raise ValueError(f"{self.status} must not carry PatentRecord entries")

    def to_dict(self) -> dict[str, Any]:
        label = _STATUS_LABELS[self.status]
        notes = [PATENT_DISCLAIMER]
        if self.status == "searched_none":
            notes.append("在所列数据库与检索范围内未发现记录,不代表不存在。")
        if self.status == "not_searched":
            notes.append("尚未检索,不可视为未发现。")
        payload = {
            "query": self.query,
            "database": self.database,
            "scope": self.scope,
            "status": self.status,
            "status_label": label,
            "searched_at": self.searched_at,
            "record_count": len(self.records),
            "records": [record.to_dict() for record in self.records],
            "disclaimer": PATENT_DISCLAIMER,
            "notes": notes,
        }
        return guarded(payload, [label, *notes])


def load_patent_records(path: str | Path) -> list[PatentRecord]:
    """Import patent records from CSV/JSON. Every row must carry source_name and retrieved_at."""
    records = []
    for index, row in enumerate(read_rows(path), start=1):
        evidence = evidence_from_row(row, row_no=index)
        try:
            records.append(
                PatentRecord(
                    patent_no=row.get("patent_no") or "",
                    title=row.get("title") or "",
                    evidence=evidence,
                    related_asin=clean_text(row.get("related_asin")),
                    related_product=clean_text(row.get("related_product")),
                    filed_date=clean_text(row.get("filed_date")),
                    publication_date=clean_text(row.get("publication_date")),
                    search_query=clean_text(row.get("search_query")),
                )
            )
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc
    return records


def load_patent_search_logs(
    path: str | Path, records: Iterable[PatentRecord] = ()
) -> list[PatentSearchLog]:
    """Import search logs; records attach to a searched_found log when search_query == query."""
    pool = list(records)
    logs = []
    for index, row in enumerate(read_rows(path), start=1):
        query = clean_text(row.get("query")) or ""
        status = clean_text(row.get("status")) or ""
        matched = tuple(r for r in pool if r.search_query == query) if status == "searched_found" else ()
        try:
            logs.append(
                PatentSearchLog(
                    query=query,
                    database=row.get("database") or "",
                    scope=row.get("scope") or "",
                    status=status,
                    searched_at=clean_text(row.get("searched_at")),
                    records=matched,
                )
            )
        except ValueError as exc:
            raise ValueError(f"row {index}: {exc}") from exc
    return logs


def summarize_patent_ledger(logs: Iterable[PatentSearchLog]) -> dict[str, Any]:
    """Count searches per status; 'not_searched' stays separate from 'searched_none'."""
    items = list(logs)
    counts = {status: sum(1 for log in items if log.status == status) for status in SEARCH_STATUSES}
    return {
        "status_counts": counts,
        "searches": [log.to_dict() for log in items],
        "disclaimer": PATENT_DISCLAIMER,
    }
