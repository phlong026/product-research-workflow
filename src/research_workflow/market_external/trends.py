"""Google Trends export import and report-excerpt import (R05).

No network access: the caller supplies exported CSV/JSON files. The metadata that
defines the Trends scope (geo, timeframe, keywords) must match before series may be
placed on the same chart.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .evidence import ExternalEvidence


@dataclass(frozen=True)
class TrendSeries:
    keyword: str
    monthly: tuple[tuple[str, float], ...]  # (YYYY-MM, mean index in the month)
    yoy: tuple[tuple[str, float | None], ...]  # (YYYY-MM, v / v_12m_ago - 1); None if no base


@dataclass(frozen=True)
class GoogleTrendsExport:
    geo: str
    timeframe: str
    keywords: tuple[str, ...]
    series: tuple[TrendSeries, ...]
    source_name: str
    source_url: str
    as_of: str


def _month(raw_date: str) -> str:
    text = raw_date.strip()
    try:
        parsed = date.fromisoformat(text) if len(text) == 10 else date.fromisoformat(text + "-01")
    except ValueError as exc:
        raise ValueError(f"unsupported date in Trends export: {raw_date!r}") from exc
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _prior_year(month: str) -> str:
    return f"{int(month[:4]) - 1:04d}-{month[5:]}"


def parse_trends_csv(
    text: str,
    *,
    geo: str,
    timeframe: str,
    source_name: str,
    source_url: str,
    as_of: str,
    keywords: Iterable[str] | None = None,
) -> GoogleTrendsExport:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"date", "keyword", "value"} <= set(reader.fieldnames):
        raise ValueError("Trends CSV needs columns: date,keyword,value")
    buckets: dict[str, dict[str, list[float]]] = {}
    for row in reader:
        keyword = row["keyword"].strip()
        if not keyword:
            raise ValueError("Trends CSV row without keyword")
        try:
            value = float(row["value"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric Trends value for {keyword}: {row['value']!r}") from exc
        buckets.setdefault(keyword, defaultdict(list))[_month(row["date"])].append(value)
    if not buckets:
        raise ValueError("Trends CSV has no data rows")
    found = tuple(buckets)
    if keywords is not None and set(keywords) != set(found):
        raise ValueError(f"declared keywords {sorted(set(keywords))} != CSV keywords {sorted(found)}")

    series = []
    for keyword, months in buckets.items():
        monthly = {m: sum(v) / len(v) for m, v in sorted(months.items())}
        yoy = []
        for month, value in monthly.items():
            base = monthly.get(_prior_year(month))
            yoy.append((month, None if not base else value / base - 1))
        series.append(TrendSeries(keyword, tuple(monthly.items()), tuple(yoy)))
    return GoogleTrendsExport(
        geo=geo,
        timeframe=timeframe,
        keywords=found,
        series=tuple(series),
        source_name=source_name,
        source_url=source_url,
        as_of=as_of,
    )


def load_trends_csv(path: str | Path, **kwargs: Any) -> GoogleTrendsExport:
    return parse_trends_csv(Path(path).read_text(encoding="utf-8"), **kwargs)


def check_trends_consistency(exports: Iterable[GoogleTrendsExport]) -> list[str]:
    """Return human-readable mismatches of geo/timeframe/keywords; empty list means consistent."""
    items = list(exports)
    if len(items) < 2:
        return []
    first = items[0]
    mismatches: list[str] = []
    for index, other in enumerate(items[1:], start=1):
        if other.geo != first.geo:
            mismatches.append(f"export {index}: geo {other.geo!r} != {first.geo!r}")
        if other.timeframe != first.timeframe:
            mismatches.append(f"export {index}: timeframe {other.timeframe!r} != {first.timeframe!r}")
        if set(other.keywords) != set(first.keywords):
            mismatches.append(
                f"export {index}: keywords {sorted(other.keywords)} != {sorted(first.keywords)}"
            )
    return mismatches


def combine_for_chart(exports: Iterable[GoogleTrendsExport]) -> tuple[TrendSeries, ...]:
    items = list(exports)
    mismatches = check_trends_consistency(items)
    if mismatches:
        raise ValueError("Trends scope inconsistent, refuse to chart together: " + "; ".join(mismatches))
    return tuple(s for e in items for s in e.series)


def trends_to_evidence(export: GoogleTrendsExport) -> list[ExternalEvidence]:
    rows: list[ExternalEvidence] = []
    for series in export.series:
        for month, value in series.monthly:
            rows.append(
                ExternalEvidence(
                    claim_id=f"trends:{export.geo}:{series.keyword}:{month}",
                    metric="google_trends_index",
                    value=value,
                    unit="index_0_100",
                    period=month,
                    source_name=export.source_name,
                    source_url=export.source_url,
                    as_of=export.as_of,
                    retrieved_from="file",
                )
            )
        for month, yoy in series.yoy:
            if yoy is not None:
                rows.append(
                    ExternalEvidence(
                        claim_id=f"trends:{export.geo}:{series.keyword}:{month}:yoy",
                        metric="google_trends_yoy",
                        value=yoy,
                        unit="ratio",
                        period=month,
                        source_name=export.source_name,
                        source_url=export.source_url,
                        as_of=export.as_of,
                        retrieved_from="file",
                    )
                )
    return rows


def parse_report_excerpts(rows: Iterable[Mapping[str, Any]]) -> list[ExternalEvidence]:
    """Rows: excerpt, source_name, date, url, optional claim_id and status (present|inaccessible)."""
    evidence: list[ExternalEvidence] = []
    for index, row in enumerate(rows, start=1):
        status = str(row.get("status") or "present").strip()
        if status not in ("present", "inaccessible"):
            raise ValueError(f"report excerpt row {index}: status must be present or inaccessible")
        excerpt = str(row.get("excerpt") or "").strip()
        when = str(row.get("date") or "").strip()
        evidence.append(
            ExternalEvidence(
                claim_id=str(row.get("claim_id") or f"report:{index}").strip(),
                metric="report_excerpt",
                value=excerpt if status == "present" else None,
                unit="text",
                period=when,
                source_name=str(row.get("source_name") or "").strip(),
                source_url=str(row.get("url") or "").strip(),
                as_of=when,
                retrieved_from="manual",
                status=status,
            )
        )
    return evidence


def import_report_excerpts(path: str | Path) -> list[ExternalEvidence]:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data["excerpts"] if isinstance(data, dict) else data
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    return parse_report_excerpts(rows)
