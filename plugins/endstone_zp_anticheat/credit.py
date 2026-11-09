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


def is_retaliation_allowed(last_hit_at: object, now: object, window_seconds: object = 5.0) -> bool:
    """Return true only when this player was hit by the same opponent within the grace window."""
    try:
        last_hit = float(last_hit_at)
        current = float(now)
        window = max(0.0, float(window_seconds))
    except (TypeError, ValueError, OverflowError):
        return False
    return last_hit > 0.0 and 0.0 <= current - last_hit <= window


def accrue_public_credit(
    score: object,
    recovery_progress_seconds: object,
    online_delta_seconds: object,
    points_per_hour: object = 0.5,
) -> tuple[float, float]:
    """Accrue recovery from active online time; progress is a persisted sub-hour remainder."""
    current = clamp_public_credit(score)
    try:
        progress = max(0.0, min(3599.99, float(recovery_progress_seconds)))
        delta = max(0.0, float(online_delta_seconds))
        rate = max(0.0, float(points_per_hour))
    except (TypeError, ValueError, OverflowError):
        return current, 0.0
    progress += delta
    completed_hours = int(progress // 3600.0)
    if completed_hours:
        current = min(100.0, current + completed_hours * rate)
        progress -= completed_hours * 3600.0
    return current, min(3599.99, progress)


def apply_public_credit_penalty(score: object, penalty: object) -> float:
    try:
        amount = max(0.0, float(penalty))
    except (TypeError, ValueError, OverflowError):
        amount = 0.0
    return max(0.0, clamp_public_credit(score) - amount)
