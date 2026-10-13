"""与人工结果逐项对账(验收用)。

参考表(CSV/XLSX/JSON)列:key, expected, [tolerance_abs], [tolerance_pct]
key 形如:
  market.2026-01.total_sales
  segments.2026-01.nugget.sales
  concentration.2026-01.top3_asin_share_pct
  rank_layers.2026-01.10.sales            (累计 TopN)
  rank_bands.2026-01.head_1_5.sales       (头/腰/尾区间)
  point_rank.2026-01.3.sales              (第 3 名自身销量,非累计)
  new_products.summary.retention_rate_pct
实际值缺失(None/不存在)判为 MISSING,绝不当作 0;容差默认 0(必须完全相等)。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

# 每个分节:取自哪份结果、哪些字段组成 key 的“维度”(月份之后)
_SECTIONS = {
    "market": ("market", "market", ()),
    "segments": ("market", "segments", ("segment",)),
    "seller_regions": ("market", "seller_regions", ("seller_region",)),
    "concentration": ("competition", "concentration", ()),
    "rank_layers": ("competition", "rank_layers", ("cutoff",)),
    "rank_bands": ("competition", "rank_bands", ("band",)),
    "head_suction": ("competition", "head_suction", ()),
}


def flatten_metrics(
    market: dict[str, Any] | None, competition: dict[str, Any] | None, new_products: dict[str, Any] | None
) -> dict[str, Any]:
    results = {"market": market or {}, "competition": competition or {}}
    flat: dict[str, Any] = {}
    for section, (source, field, dims) in _SECTIONS.items():
        for row in results[source].get(field) or []:
            prefix = [section, str(row.get("month", ""))] + [str(row.get(d, "")) for d in dims]
            for name, value in row.items():
                if name == "month" or name in dims:
                    continue
                if isinstance(value, (int, float)) or value is None:
                    flat[".".join([*prefix, name])] = value
    # 单点名次矩阵:point_sales["3"] 是第 3 名自身销量;None=该位次不存在(未知,不是 0)
    for row in ((competition or {}).get("point_rank_matrix") or {}).get("rows") or []:
        for rank, sales in (row.get("point_sales") or {}).items():
            flat[f"point_rank.{row.get('month', '')}.{rank}.sales"] = sales
    for name, value in ((new_products or {}).get("summary") or {}).items():
        if isinstance(value, (int, float)) or value is None:
            flat[f"new_products.summary.{name}"] = value
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
