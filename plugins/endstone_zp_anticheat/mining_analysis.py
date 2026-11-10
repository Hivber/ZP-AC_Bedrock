"""Conservative mining-pattern summaries. This is detection, not packet obfuscation."""
from __future__ import annotations

from typing import Iterable

ORE_TYPES = frozenset({
    "coal_ore", "deepslate_coal_ore", "copper_ore", "deepslate_copper_ore",
    "iron_ore", "deepslate_iron_ore", "gold_ore", "deepslate_gold_ore",
    "redstone_ore", "deepslate_redstone_ore", "lapis_ore", "deepslate_lapis_ore",
    "diamond_ore", "deepslate_diamond_ore", "emerald_ore",
    "deepslate_emerald_ore", "ancient_debris",
})
RARE_ORES = frozenset({
    "diamond_ore", "deepslate_diamond_ore", "emerald_ore",
    "deepslate_emerald_ore", "ancient_debris",
})
ROCK_TOKENS = (
    "stone", "deepslate", "netherrack", "blackstone", "basalt",
    "tuff", "granite", "diorite", "andesite", "calcite", "gravel",
)


def classify_mined_block(block_type: str) -> str | None:
    name = str(block_type or "").lower().split(":")[-1]
    if name in ORE_TYPES:
        return "ore"
    if any(token in name for token in ROCK_TOKENS):
        return "rock"
    return None


def summarize_mining_events(rows: Iterable[tuple], cluster_distance: float = 5.5) -> dict:
    events = list(rows)
    ores = [row for row in events if len(row) >= 7 and row[6] == "ore"]
    rocks = [row for row in events if len(row) >= 7 and row[6] == "rock"]
    rare = sum(1 for row in ores if str(row[5]).lower() in RARE_ORES)
    anchors: list[tuple[float, float, float]] = []
    distance_limit = max(3.0, float(cluster_distance))
    for row in ores:
        point = (float(row[2]), float(row[3]), float(row[4]))
        if all(
            (point[0] - anchor[0]) ** 2
            + (point[1] - anchor[1]) ** 2
            + (point[2] - anchor[2]) ** 2 > distance_limit ** 2
            for anchor in anchors
        ):
            anchors.append(point)
    total = len(ores) + len(rocks)
    return {
        "rock_count": len(rocks),
        "ore_count": len(ores),
        "rare_count": rare,
        "cluster_count": len(anchors),
        "ore_ratio": len(ores) / total if total else 0.0,
        "ore_types": sorted({str(row[5]).lower() for row in ores}),
        "cluster_anchors": anchors[:12],
    }
