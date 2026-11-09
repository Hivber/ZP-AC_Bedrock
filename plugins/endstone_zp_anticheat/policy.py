"""Pure punishment-corroboration policy helpers for ZP-AC."""

from collections.abc import Iterable


# Two rules from the same gameplay domain are not independent corroboration.
STRONG_RULE_DOMAINS = {
    "speed_sustained": "movement",
    "hover_confirmed": "movement",
    "reach_extreme": "combat",
    "multi_target_combat": "combat",
}


def independent_strong_domain_count(strong_codes: Iterable[str]) -> int:
    """Count distinct strong behavior domains, ignoring weak/unknown rule codes."""
    return len({
        STRONG_RULE_DOMAINS[code]
        for code in strong_codes
        if code in STRONG_RULE_DOMAINS
    })


def has_independent_strong_corroboration(
    strong_codes: Iterable[str], minimum_domains: int = 2
) -> bool:
    """Return true only when enough independent strong behavior domains agree."""
    required = max(2, min(4, int(minimum_domains)))
    return independent_strong_domain_count(strong_codes) >= required
