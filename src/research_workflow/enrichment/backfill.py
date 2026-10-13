"""Targeted backfill: seller_region for gaps (T1-6) and bullets once per unique ASIN (T1-7).

Both functions reuse the providers already configured on ``ASINEnrichmentService``
(primary + fallbacks) and call them through ``with_retries``. Disagreements between
sources are recorded as conflicts, never silently overwritten.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from research_workflow.connectors.base import safe_exception_summary, with_retries

from .adapters import profile_from_payload, split_region_and_account_type
from .models import (
    ASINProfile,
    BulletBackfill,
    EnrichmentConflict,
    EnrichmentFailure,
    EnrichmentReport,
)
from .service import ASINEnrichmentService

logger = logging.getLogger(__name__)

_MISSING_REGIONS = {"", "UNKNOWN"}


def _clean_profile(profile: ASINProfile) -> ASINProfile:
    """Copy of the profile with region normalized (CN(HK)->CN) and VC moved out of it."""
    region, account_type = split_region_and_account_type(profile.seller_region)
    attributes = dict(profile.attributes)
    if account_type:
        attributes.setdefault("account_type", account_type)
    return replace(
        profile,
        seller_region=region,
        bullets=list(profile.bullets),
        attributes=attributes,
        sources=list(profile.sources),
    )


def _as_profile_map(profiles: Mapping[str, ASINProfile] | Iterable[ASINProfile]) -> dict[str, ASINProfile]:
    items = profiles.values() if isinstance(profiles, Mapping) else profiles
    return {p.asin.strip().upper(): _clean_profile(p) for p in items}


async def enrich_missing_regions(
    profiles: Mapping[str, ASINProfile] | Iterable[ASINProfile],
    service: ASINEnrichmentService,
    marketplace: str = "US",
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.0,
) -> EnrichmentReport:
    """Backfill seller_region only for ASINs where it is missing or UNKNOWN.

    Every provider is queried (in primary-then-fallback order) so disagreements can be
    detected. The first non-UNKNOWN value wins; if other sources disagree the conflict is
    recorded in ``report.conflicts`` for human review.
    """
    current = _as_profile_map(profiles)
    total = len(current)
    targets = [asin for asin, p in current.items() if p.seller_region in _MISSING_REGIONS]
    unknown_before = len(targets)
    providers = [service.primary, *service.fallbacks]
    semaphore = asyncio.Semaphore(service.concurrency)

    conflicts: list[EnrichmentConflict] = []
    failures: list[EnrichmentFailure] = []

    async def lookup(asin: str) -> tuple[dict[str, str], str | None, list[EnrichmentFailure]]:
        regions: dict[str, str] = {}
        account_type: str | None = None
        errs: list[EnrichmentFailure] = []
        async with semaphore:
            for provider in providers:
                try:
                    payload = await with_retries(
                        lambda p=provider: p.get_product_detail(asin, marketplace),
                        attempts=attempts,
                        base_delay_seconds=base_delay_seconds,
                        operation=f"region backfill {provider.name}",
                    )
                except Exception as exc:  # noqa: BLE001 - recorded in report, never swallowed
                    errs.append(EnrichmentFailure(asin, provider.name, str(exc)))
                    logger.warning(
                        "region backfill failed asin=%s provider=%s: %s",
                        asin, provider.name, safe_exception_summary(exc),
                    )
                    continue
                profile = profile_from_payload(payload, asin=asin, source=provider.name)
                if profile is None:
                    continue
                if profile.seller_region not in _MISSING_REGIONS:
                    regions[provider.name] = profile.seller_region
                account_type = account_type or profile.attributes.get("account_type")
        return regions, account_type, errs

    rows = await asyncio.gather(*(lookup(asin) for asin in targets))

    filled = 0
    for asin, (regions, account_type, errs) in zip(targets, rows):
        failures.extend(errs)
        profile = current[asin]
        if account_type:
            profile.attributes.setdefault("account_type", account_type)
        if not regions:
            continue
        source, chosen = next(iter(regions.items()))
        profile.seller_region = chosen
        if source not in profile.sources:
            profile.sources.append(source)
        filled += 1
        if len(set(regions.values())) > 1:
            conflicts.append(EnrichmentConflict(asin, "seller_region", dict(regions), chosen))
            logger.warning(
                "backfill conflict asin=%s field=seller_region sources=%s chosen=%s",
                asin, sorted(regions), chosen,
            )

    unknown_after = sum(1 for p in current.values() if p.seller_region in _MISSING_REGIONS)
    return EnrichmentReport(
        profiles=current,
        total_profiles=total,
        targeted=len(targets),
        filled=filled,
        unknown_before=unknown_before,
        unknown_after=unknown_after,
        conflicts=conflicts,
        failures=failures,
    )


def _record_asin(record: Any) -> str:
    if isinstance(record, str):
        return record.strip().upper()
    if isinstance(record, Mapping):
        return str(record.get("asin") or record.get("ASIN") or "").strip().upper()
    return str(getattr(record, "asin", "") or "").strip().upper()


def _record_dict(record: Any, asin: str) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    if hasattr(record, "to_dict"):
        return dict(record.to_dict())
    return {"asin": asin}


def _record_bullets(record: Any) -> list[str]:
    value = record.get("bullets") if isinstance(record, Mapping) else getattr(record, "bullets", None)
    return [str(b) for b in value] if isinstance(value, (list, tuple)) else []


async def enrich_bullets_unique_asins(
    records_by_month: Mapping[str, Iterable[Any]] | Iterable[Any],
    service: ASINEnrichmentService,
    marketplace: str = "US",
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.0,
) -> BulletBackfill:
    """Fetch bullets once per unique ASIN, then backfill every month it appears in.

    ``records_by_month`` maps month -> records (str ASIN, mapping, or object with ``asin``);
    a plain iterable of ASINs is also accepted (then ``filled_records`` is keyed by "").
    Records that already carry bullets are kept as-is; if fetched bullets differ the
    disagreement is recorded as a conflict instead of overwriting.
    """
    by_month: Mapping[str, Iterable[Any]] = (
        records_by_month if isinstance(records_by_month, Mapping) else {"": records_by_month}
    )
    month_records = {m: list(recs) for m, recs in by_month.items()}

    # Table 1 -> table 2: unique ASINs that are missing bullets in at least one record.
    unique: dict[str, None] = {}
    needs_fetch: dict[str, None] = {}
    for recs in month_records.values():
        for rec in recs:
            asin = _record_asin(rec)
            if not asin:
                continue
            unique.setdefault(asin)
            if not _record_bullets(rec):
                needs_fetch.setdefault(asin)

    providers = [service.primary, *service.fallbacks]
    semaphore = asyncio.Semaphore(service.concurrency)
    failures: list[EnrichmentFailure] = []

    async def fetch(asin: str) -> tuple[list[str], list[EnrichmentFailure]]:
        errs: list[EnrichmentFailure] = []
        async with semaphore:
            for provider in providers:
                try:
                    payload = await with_retries(
                        lambda p=provider: p.get_product_detail(asin, marketplace),
                        attempts=attempts,
                        base_delay_seconds=base_delay_seconds,
                        operation=f"bullets backfill {provider.name}",
                    )
                except Exception as exc:  # noqa: BLE001 - recorded in report, never swallowed
                    errs.append(EnrichmentFailure(asin, provider.name, str(exc)))
                    logger.warning(
                        "bullets backfill failed asin=%s provider=%s: %s",
                        asin, provider.name, safe_exception_summary(exc),
                    )
                    continue
                profile = profile_from_payload(payload, asin=asin, source=provider.name)
                if profile is not None and profile.bullets:
                    return profile.bullets, errs
        return [], errs

    fetch_order = list(needs_fetch)
    results = await asyncio.gather(*(fetch(asin) for asin in fetch_order))
    bullets_by_asin: dict[str, list[str]] = {}
    for asin, (bullets, errs) in zip(fetch_order, results):
        failures.extend(errs)
        if bullets:
            bullets_by_asin[asin] = bullets

    conflicts: list[EnrichmentConflict] = []
    filled_records: dict[str, list[dict[str, Any]]] = {}
    for month, recs in month_records.items():
        out: list[dict[str, Any]] = []
        for rec in recs:
            asin = _record_asin(rec)
            row = _record_dict(rec, asin)
            existing = _record_bullets(rec)
            fetched = bullets_by_asin.get(asin)
            if existing:
                row["bullets"] = existing
                if fetched and fetched != existing:
                    conflicts.append(
                        EnrichmentConflict(
                            asin,
                            "bullets",
                            {"existing": " | ".join(existing), "fetched": " | ".join(fetched)},
                            chosen="existing",
                            month=month or None,
                        )
                    )
                    logger.warning(
                        "backfill conflict asin=%s field=bullets month=%s chosen=existing",
                        asin, month or "-",
                    )
            elif fetched:
                row["bullets"] = list(fetched)
            out.append(row)
        filled_records[month] = out

    return BulletBackfill(
        bullets_by_asin=bullets_by_asin,
        filled_records=filled_records,
        unique_asins=len(unique),
        fetched_asins=len(fetch_order),
        missing_asins=[a for a in fetch_order if a not in bullets_by_asin],
        conflicts=conflicts,
        failures=failures,
    )
