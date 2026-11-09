"""Conservative potion-effect helpers for comparable movement sampling."""
from __future__ import annotations

from typing import Any


def read_effect_amplifier(player: Any, effect_id: str) -> int | None:
    """Return -1 when absent, amplifier >= 0 when active, None when unreadable."""
    getter = getattr(player, "get_effect", None)
    if not callable(getter):
        return None
    try:
        effect = getter(effect_id)
        if effect is None:
            return -1
        amplifier = int(effect.amplifier)
        if amplifier < 0:
            return None
        return min(amplifier, 10)
    except Exception:
        return None


def speed_effect_multiplier(amplifier: int) -> float:
    """Bedrock Speed effect: conservatively scale by 20% per amplifier level."""
    if amplifier < 0:
        return 1.0
    return min(3.0, 1.0 + 0.2 * (min(int(amplifier), 10) + 1))
