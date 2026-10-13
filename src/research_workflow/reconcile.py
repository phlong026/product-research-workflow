"""与人工结果逐项对账。

参考表（CSV/XLSX/JSON）列：key, expected, [tolerance_abs], [tolerance_pct]
key 形如  market.2026-01.total_sales / concentration.2026-01.top3_asin_share_pct /
          rank_points.2026-01.3.sales / new_products.summary.retention_rate_pct
实际值缺失（None/不存在）判为 MISSING，绝不当作 0；容差默认 0（必须完全相等）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

DISCRIMINATORS = {
    "market": (), "segments": ("segment",), "seller_regions": ("seller_region",),
    "concentration": (), "rank_points": ("rank",), "rank_layers": ("cutoff",),
    "rank_bands": ("band",), "head_suction": (),
}


def flatten_metrics(
    market: dict[str, Any] | None, competition: dict[str, Any] | None, new_products: dict[str, Any] | None
) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    sources = {
        "market": (market or {}).get("market"), "segments": (market or {}).get("segments"),
        "seller_regions": (market or {}).get("seller_regions"),
        "concentration": (competition or {}).get("concentration"), "rank_points": (competition or {}).get("rank_points"),
        "rank_layers": (competition or {}).get("rank_layers"), "rank_bands": (competition or {}).get("rank_bands"),
        "head_suction": (competition or {}).get("head_suction"),
    }
    for section, rows in sources.items():
        for row in rows or []:
            prefix = [section, str(row.get("month", ""))] + [str(row.get(d, "")) for d in DISCRIMINATORS[section]]
            for field, value in row.items():
                if field == "month" or field in DISCRIMINATORS[section]:
                    continue
                if isinstance(value, (int, float)) or value is None:
                    flat[".".join([*prefix, field])] = value
    for field, value in ((new_products or {}).get("summary") or {}).items():
        if isinstance(value, (int, float)) or value is None:
            flat[f"new_products.summary.{field}"] = value
    return flat


@dataclass(slots=True, frozen=True)
class ReconcileRow:
    key: str
    expected: float | None
    actual: float | None
    status: str  # MATCH | MISMATCH | MISSING
    diff: float | None
    tolerance_abs: float
    tolerance_pct: float | None


def _num(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def reconcile(reference_rows: Iterable[dict[str, Any]], flat: dict[str, Any]) -> list[ReconcileRow]:
    out: list[ReconcileRow] = []
    seen: set[str] = set()
    for row in reference_rows:
        key = str(row.get("key", "")).strip()
        if not key:
            continue
        if key in seen:
            raise ValueError(f"参考表中 key 重复: {key}")
        seen.add(key)
        expected = _num(row.get("expected"))
        if expected is None:
            raise ValueError(f"参考值缺失或非法: {key}")
        tol_abs = _num(row.get("tolerance_abs")) or 0.0
        tol_pct = _num(row.get("tolerance_pct"))
        actual = _num(flat.get(key))
        if actual is None:
            out.append(ReconcileRow(key, expected, None, "MISSING", None, tol_abs, tol_pct))
            continue
        diff = actual - expected
        allowed = max(tol_abs, abs(expected) * tol_pct / 100.0 if tol_pct is not None else 0.0)
        out.append(ReconcileRow(key, expected, actual, "MATCH" if abs(diff) <= allowed + 1e-9 else "MISMATCH",
                                diff, tol_abs, tol_pct))
    return out


def summarize(rows: list[ReconcileRow]) -> dict[str, int]:
    return {"total": len(rows), **{s: sum(1 for r in rows if r.status == s) for s in ("MATCH", "MISMATCH", "MISSING")}}


def to_dicts(rows: list[ReconcileRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]
