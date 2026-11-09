"""Public reputation score policy, separate from hidden anti-cheat trust."""
from __future__ import annotations

PUBLIC_CREDIT_DEFAULT = 100.0
PUBLIC_CREDIT_LOCK_THRESHOLD = 70.0


def clamp_public_credit(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return PUBLIC_CREDIT_DEFAULT
    if score != score or score in (float("inf"), float("-inf")):
        return PUBLIC_CREDIT_DEFAULT
    return max(0.0, min(100.0, score))


def can_damage_players(score: object, threshold: object = PUBLIC_CREDIT_LOCK_THRESHOLD, exempt: bool = False) -> bool:
    if exempt:
        return True
    try:
        limit = max(0.0, min(100.0, float(threshold)))
    except (TypeError, ValueError, OverflowError):
        limit = PUBLIC_CREDIT_LOCK_THRESHOLD
    return clamp_public_credit(score) >= limit


def recover_public_credit(
    score: object,
    active_seconds: object,
    points_per_hour: object,
    seconds_since_penalty: object,
    recovery_delay_seconds: object,
) -> float:
    current = clamp_public_credit(score)
    try:
        active = max(0.0, float(active_seconds))
        rate = max(0.0, float(points_per_hour))
        elapsed = max(0.0, float(seconds_since_penalty))
        delay = max(0.0, float(recovery_delay_seconds))
    except (TypeError, ValueError, OverflowError):
        return current
    if elapsed < delay or active <= 0.0 or rate <= 0.0:
        return current
    return min(100.0, current + active * rate / 3600.0)


def apply_public_credit_penalty(score: object, penalty: object) -> float:
    try:
        amount = max(0.0, float(penalty))
    except (TypeError, ValueError, OverflowError):
        amount = 0.0
    return max(0.0, clamp_public_credit(score) - amount)
