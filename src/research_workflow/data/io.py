from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable


def read_tabular_rows(path: str | Path, *, sheet_name: str | None = None) -> list[dict[str, Any]]:
    """Read CSV/JSON/XLSX into row dictionaries.

    XLSX uses Polars+calamine when the `data` extra is installed. This keeps the
    production reader fast while core transformation tests remain dependency-free.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [dict(row) for row in payload]
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return [dict(row) for row in payload["rows"]]
        raise ValueError("JSON input must be a list of rows or {'rows': [...]} object")
    if suffix in {".xlsx", ".xlsm"}:
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError("Install data extras for Excel: pip install -e '.[data]'") from exc
        kwargs: dict[str, Any] = {"engine": "calamine"}
        if sheet_name:
            kwargs["sheet_name"] = sheet_name
        frame = pl.read_excel(path, **kwargs)
        return frame.to_dicts()
    raise ValueError(f"Unsupported tabular input: {path.suffix}")


def write_json_rows(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_csv_rows(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
