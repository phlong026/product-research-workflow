"""Deterministic growth arithmetic shared by the TikTok and market-size modules."""

from __future__ import annotations


def cagr(start: float, end: float, periods: float) -> float:
    """CAGR = (end / start) ** (1 / periods) - 1."""
    if periods <= 0:
        raise ValueError("periods must be positive")
    if start <= 0 or end < 0:
        raise ValueError("CAGR needs start > 0 and end >= 0")
    return (end / start) ** (1 / periods) - 1


def compound(base: float, rate: float, periods: float) -> float:
    """base * (1 + rate) ** periods."""
    return base * (1 + rate) ** periods
