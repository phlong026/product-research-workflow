"""Shared evidence ledger primitives for the external competition streams (R21/R23/R24/R28).

This package never goes online: callers pass data in (objects, CSV or JSON) and every
record must carry its source, date, link or file reference and search scope.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from research_workflow.analysis.market import FORBIDDEN_DECISION_PATTERNS

# Extra judgement words specific to this package (legal / business verdict wording).
EXTRA_FORBIDDEN_PATTERNS = (
    r"侵权",
    r"可以进入",
    r"应该进入",
    r"不应进入",
)
ALL_FORBIDDEN_PATTERNS = FORBIDDEN_DECISION_PATTERNS + EXTRA_FORBIDDEN_PATTERNS


def assert_no_decision_language(texts: Iterable[str]) -> None:
    """Raise ValueError if system-generated text contains a verdict / legal-conclusion word."""
    joined = "\n".join(texts)
    for pattern in ALL_FORBIDDEN_PATTERNS:
        if re.search(pattern, joined, flags=re.IGNORECASE):
            raise ValueError(f"Generated text crossed the human decision boundary: {pattern}")


def guarded(payload: dict[str, Any], generated_texts: Iterable[str]) -> dict[str, Any]:
    """Return payload after scanning the system-generated strings that went into it."""
    assert_no_decision_language(generated_texts)
    return payload


def sha256_of_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Where a piece of evidence came from. Requires a locator: url or file_path."""

    source_name: str
    retrieved_at: str
    url: str | None = None
    file_path: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not clean_text(self.source_name):
            raise ValueError("EvidenceRef.source_name is required")
        if not clean_text(self.retrieved_at):
            raise ValueError("EvidenceRef.retrieved_at is required")
        if not (clean_text(self.url) or clean_text(self.file_path)):
            raise ValueError("EvidenceRef requires url or file_path")
        if self.sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise ValueError("EvidenceRef.sha256 must be 64 hex chars")
        if self.sha256 and self.file_path and Path(self.file_path).is_file():
            if sha256_of_file(self.file_path).lower() != self.sha256.lower():
                raise ValueError(f"SHA256 mismatch for {self.file_path}")

    @classmethod
    def from_file(cls, source_name: str, file_path: str | Path, retrieved_at: str) -> "EvidenceRef":
        """Build a file reference whose SHA256 is computed from the file on disk."""
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"Evidence file not found: {path}")
        return cls(source_name, retrieved_at, file_path=str(path), sha256=sha256_of_file(path))

    def verify(self) -> str:
        """Return one of: no_file | no_hash | file_missing | match | mismatch."""
        if not self.file_path:
            return "no_file"
        if not self.sha256:
            return "no_hash"
        path = Path(self.file_path)
        if not path.is_file():
            return "file_missing"
        return "match" if sha256_of_file(path).lower() == self.sha256.lower() else "mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "url": self.url,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "file_check": self.verify(),
        }


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d")


def parse_date(value: Any) -> date | None:
    """Parse a date leniently; return None for empty or invalid input (never guess)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if text is None:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
        text = text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_rows(path: str | Path) -> list[dict[str, Any]]:
    """Read records from CSV (header row) or JSON (list, or {"records": [...]})."""
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"Import file not found: {file}")
    if file.suffix.lower() == ".json":
        data = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(data, Mapping):
            data = data.get("records")
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise ValueError(f"JSON import must be a list of objects: {file}")
        return [dict(row) for row in data]
    with open(file, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def evidence_from_row(row: Mapping[str, Any], *, row_no: int, prefix: str = "") -> EvidenceRef:
    """Build EvidenceRef from source_name/url/file_path/sha256/retrieved_at columns.

    Missing source information raises ValueError naming the row.
    """

    def get(key: str) -> str | None:
        return clean_text(row.get(f"{prefix}{key}"))

    source_name = get("source_name")
    if not source_name:
        raise ValueError(f"row {row_no}: missing {prefix}source_name")
    retrieved_at = get("retrieved_at")
    if not retrieved_at:
        raise ValueError(f"row {row_no}: missing {prefix}retrieved_at")
    try:
        return EvidenceRef(
            source_name=source_name,
            retrieved_at=retrieved_at,
            url=get("url") or get("source_url"),
            file_path=get("file_path"),
            sha256=get("sha256"),
        )
    except ValueError as exc:
        raise ValueError(f"row {row_no}: {exc}") from exc


def require_text(value: Any, field_name: str) -> str:
    text = clean_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def optional_float(value: Any) -> float | None:
    """Parse a number; empty or unparsable input is None (missing), never 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None
