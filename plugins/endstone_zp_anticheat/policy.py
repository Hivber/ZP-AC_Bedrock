"""Conservative enforcement policy helpers for ZP-AC."""
from __future__ import annotations

from typing import Iterable

STRONG_RULE_CODES = frozenset({
    "speed_sustained",
    "hover_confirmed",
    "reach_extreme",
    "multi_target_combat",
})


def has_independent_strong_evidence(
    rule_codes: Iterable[str],
    strong_codes: Iterable[str],
    minimum: int = 2,
) -> bool:
    """Require at least two distinct, recognized high-confidence rule families."""
    required = max(2, int(minimum))
    observed = set(rule_codes)
    strong = set(strong_codes) & observed & STRONG_RULE_CODES
    return len(observed) >= 2 and len(strong) >= required


def profile_penalty(points: int, count: int, high_confidence: bool) -> tuple[float, float]:
    """Return (risk increase, trust decrease); weak heuristics never penalize profiles."""
    if not high_confidence:
        return 0.0, 0.0
    risk_delta = 9.0 + min(5.0, max(0, int(points)) * 0.35)
    risk_delta += min(2.0, max(0, int(count) - 1) * 0.25)
    trust_delta = 3.0 + min(2.0, max(0, int(points)) * 0.12)
    return risk_delta, trust_delta
