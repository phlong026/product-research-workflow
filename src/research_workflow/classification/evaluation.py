"""Offline evaluation of classification output against a human-labelled gold set."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import ClassificationBatch, ProductClassification

NEEDS_REVIEW = "NEEDS_REVIEW"  # confusion-matrix column for rows left to a human


@dataclass(slots=True, frozen=True)
class EvalReport:
    total: int  # gold ASINs evaluated
    answered: int  # rows the machine committed to (not pending review)
    correct: int  # answered rows whose label equals gold
    needs_review: int  # rows pending a human (incl. missing predictions)
    accuracy: float | None  # correct / answered; None when nothing was answered
    overall_accuracy: float | None  # correct / total; pending rows count as not correct
    review_rate: float | None  # needs_review / total
    # gold label -> predicted label (or NEEDS_REVIEW) -> count
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    missing_predictions: tuple[str, ...] = ()


def evaluate(
    predictions: ClassificationBatch | Mapping[str, ProductClassification | str],
    gold: Mapping[str, str],
    *,
    unknown_label: str = "unknown",
) -> EvalReport:
    """Compare predictions with gold labels.

    A prediction is "pending" when it is flagged needs_review, carries the unknown label,
    or is absent. Pending rows are excluded from ``accuracy`` and reported via
    ``review_rate``; ``overall_accuracy`` is the conservative end-to-end number.
    Plain-string predictions are treated as committed labels.
    """
    rows = predictions.rows if isinstance(predictions, ClassificationBatch) else predictions
    rows = {asin.strip().upper(): row for asin, row in rows.items()}

    confusion: dict[str, dict[str, int]] = {}
    answered = correct = pending = 0
    missing: list[str] = []
    for raw_asin, expected in sorted(gold.items()):
        asin = raw_asin.strip().upper()
        row = rows.get(asin)
        if row is None:
            missing.append(asin)
        label, review = _unpack(row, unknown_label)
        if review:
            pending += 1
            predicted = NEEDS_REVIEW
        else:
            answered += 1
            correct += label == expected
            predicted = label
        cell = confusion.setdefault(expected, {})
        cell[predicted] = cell.get(predicted, 0) + 1

    total = len(gold)
    return EvalReport(
        total=total,
        answered=answered,
        correct=correct,
        needs_review=pending,
        accuracy=correct / answered if answered else None,
        overall_accuracy=correct / total if total else None,
        review_rate=pending / total if total else None,
        confusion={g: dict(sorted(c.items())) for g, c in sorted(confusion.items())},
        missing_predictions=tuple(missing),
    )


def _unpack(row: ProductClassification | str | None, unknown_label: str) -> tuple[str, bool]:
    if row is None:
        return "", True
    if isinstance(row, str):
        return row, row == unknown_label
    return row.label, row.needs_review or row.label == unknown_label
