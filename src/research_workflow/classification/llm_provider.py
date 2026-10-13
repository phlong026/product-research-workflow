"""LLM-backed classification provider with strict output validation.

The provider never imports an LLM SDK: callers inject ``complete``, an async callable
that takes the rendered prompt and returns a JSON string. Whatever the model returns is
treated as untrusted. A row only becomes a classification when it has

* a label that is one of the schema's regular labels,
* a confidence at or above the configured threshold,
* evidence copied verbatim from the product's title/bullets,
* no ``ambiguous`` flag and no business-decision content.

Anything else becomes ``needs_review`` (label = schema.unknown_label) for a human; the
model's suggestion, if any, is kept only inside ``review_reason``. Output is
classification only (label/confidence/evidence): never a Yes/No business recommendation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
import re
from typing import Any

from research_workflow.connectors.base import safe_exception_summary, with_retries
from research_workflow.enrichment.models import ASINProfile

from .models import ClassificationSchema
from .prompt import build_classification_prompt

logger = logging.getLogger(__name__)

_CONFIDENCE_LEVELS = {"low": 0, "medium": 1, "high": 2}
_ALLOWED_KEYS = {"asin", "label", "confidence", "evidence", "ambiguous"}
_DECISION_KEY = re.compile(
    r"decision|recommend|verdict|go_?no_?go|yes_?no|should|enter|invest|worth", re.IGNORECASE
)
# Mirrors analysis.market.FORBIDDEN_DECISION_PATTERNS. Not imported: analysis -> metrics ->
# classification would be a circular import. A test pins the two together.
FORBIDDEN_DECISION_PATTERNS = (
    r"\bYES\b",
    r"\bNO\b",
    r"建议进入",
    r"不建议进入",
    r"最终决策",
    r"go/no-go",
)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMClassificationProvider:
    name = "llm"

    def __init__(
        self,
        complete: Callable[[str], Awaitable[str]],
        *,
        name: str = "llm",
        chunk_size: int = 20,
        attempts: int = 2,
        base_delay_seconds: float = 0.0,
        min_confidence: str = "medium",
        require_grounded_evidence: bool = True,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        if min_confidence not in _CONFIDENCE_LEVELS:
            raise ValueError("min_confidence must be one of high|medium|low")
        self.complete = complete
        self.name = name
        self.chunk_size = chunk_size
        self.attempts = attempts
        self.base_delay_seconds = base_delay_seconds
        self.min_confidence = min_confidence
        self.require_grounded_evidence = require_grounded_evidence

    async def classify(
        self, schema: ClassificationSchema, profiles: list[ASINProfile]
    ) -> list[dict[str, Any]]:
        schema.validate()
        rows: list[dict[str, Any]] = []
        for start in range(0, len(profiles), self.chunk_size):
            chunk = profiles[start : start + self.chunk_size]
            try:
                raw_rows = await with_retries(
                    lambda c=chunk: self._request(schema, c),
                    attempts=self.attempts,
                    base_delay_seconds=self.base_delay_seconds,
                    operation=f"llm classify ({self.name})",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as per-row review reason
                # 只记块大小与脱敏后的异常摘要;不记 prompt / 商品标题要点全文
                logger.warning(
                    "llm classification chunk failed provider=%s chunk_start=%d chunk_size=%d: %s",
                    self.name, start, len(chunk), safe_exception_summary(exc),
                )
                rows.extend(
                    _review_row(p.asin, f"llm call failed after {self.attempts} attempts: {exc}")
                    for p in chunk
                )
                continue
            rows.extend(self._validate_chunk(schema, chunk, raw_rows))
        return rows

    async def _request(
        self, schema: ClassificationSchema, chunk: list[ASINProfile]
    ) -> list[Any]:
        prompt = build_classification_prompt(schema, chunk, require_evidence=True)
        text = await self.complete(prompt)
        return _parse_rows(text)

    def _validate_chunk(
        self, schema: ClassificationSchema, chunk: list[ASINProfile], raw_rows: list[Any]
    ) -> list[dict[str, Any]]:
        by_asin: dict[str, list[dict[str, Any]]] = {}
        for raw in raw_rows:
            if isinstance(raw, dict):
                by_asin.setdefault(str(raw.get("asin", "")).strip().upper(), []).append(raw)

        out: list[dict[str, Any]] = []
        for profile in chunk:
            asin = profile.asin.strip().upper()
            candidates = by_asin.get(asin, [])
            if not candidates:
                out.append(_review_row(asin, "model returned no row for this asin"))
            elif len({str(c.get("label")) for c in candidates}) > 1:
                out.append(_review_row(asin, "model returned conflicting duplicate rows"))
            else:
                out.append(self._validate_row(schema, profile, candidates[0]))
        return out

    def _validate_row(
        self, schema: ClassificationSchema, profile: ASINProfile, raw: dict[str, Any]
    ) -> dict[str, Any]:
        asin = profile.asin.strip().upper()
        suggested = raw.get("label")
        suggestion = f"model suggested {suggested!r}; " if isinstance(suggested, str) else ""

        if _crosses_decision_boundary(raw):
            return _review_row(asin, "model output contained business-decision content")

        regular_labels = {label.key for label in schema.labels}
        if not isinstance(suggested, str) or suggested.strip() not in regular_labels:
            reason = "label missing" if suggested is None else "label not in schema"
            return _review_row(asin, f"{reason}; {suggestion}".rstrip("; "))
        label = suggested.strip()

        confidence = _confidence_level(raw.get("confidence"))
        evidence, grounded = _evidence(raw.get("evidence"), profile)
        reason = None
        if raw.get("ambiguous") is True:
            reason = "model flagged ambiguous"
        elif confidence is None or _CONFIDENCE_LEVELS[confidence] < _CONFIDENCE_LEVELS[self.min_confidence]:
            reason = f"confidence {confidence or 'missing'} below {self.min_confidence}"
        elif not evidence:
            reason = "evidence missing"
        elif self.require_grounded_evidence and not grounded:
            reason = "evidence not found in title/bullets"
        if reason:
            row = _review_row(asin, f"{reason}; {suggestion}".rstrip("; "))
            row["evidence"] = evidence if grounded else ""
            return row

        return {
            "asin": asin,
            "label": label,
            "confidence": confidence,
            "evidence": evidence,
            "rationale": "",
            "needs_review": False,
        }


def _review_row(asin: str, reason: str) -> dict[str, Any]:
    return {
        "asin": asin.strip().upper(),
        "label": "",  # empty -> service maps to unknown_label
        "confidence": "low",
        "evidence": "",
        "rationale": "",
        "needs_review": True,
        "review_reason": reason,
    }


def _parse_rows(text: Any) -> list[Any]:
    if not isinstance(text, str):
        raise ValueError("complete() must return a JSON string")
    data = json.loads(_FENCE.sub("", text.strip()))
    if isinstance(data, dict):
        data = data.get("results", data.get("classifications"))
    if not isinstance(data, list):
        raise ValueError("model output must be a JSON array of classification objects")
    return data


def _confidence_level(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "high" if value >= 0.8 else "medium" if value >= 0.5 else "low"
    if isinstance(value, str) and value.strip().lower() in _CONFIDENCE_LEVELS:
        return value.strip().lower()
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _evidence(value: Any, profile: ASINProfile) -> tuple[str, bool]:
    """Return (evidence text, whether every snippet appears verbatim in title/bullets)."""
    snippets = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    snippets = [s.strip() for s in snippets if isinstance(s, str) and s.strip()]
    if not snippets:
        return "", False
    source = _norm(" \n ".join([profile.title, *profile.bullets]))
    return " | ".join(snippets), all(_norm(s) in source for s in snippets)


def _crosses_decision_boundary(raw: dict[str, Any]) -> bool:
    """Only the model's own extra fields are scanned; evidence is quoted source text."""
    for key, value in raw.items():
        if key in _ALLOWED_KEYS:
            continue
        if _DECISION_KEY.search(str(key)):
            return True
        if isinstance(value, str) and any(
            re.search(p, value, flags=re.IGNORECASE) for p in FORBIDDEN_DECISION_PATTERNS
        ):
            return True
    return False
