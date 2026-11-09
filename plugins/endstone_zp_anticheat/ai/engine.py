"""Local, dependency-free behavior anomaly analysis for ZP-AC.

This is a conservative online statistical model, not a pretrained neural network.
It learns per-player baselines from server-observed behavior windows and emits
review-only findings. It never kicks, bans, or changes the plugin risk score.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict, deque
from typing import Any


class LocalBehaviorAI:
    """Per-player robust baseline detector with bounded memory."""

    def __init__(
        self,
        window_seconds: float = 120.0,
        min_samples: int = 12,
        threshold: float = 3.5,
        min_features: int = 2,
        max_players: int = 10000,
    ) -> None:
        self.window_seconds = max(30.0, min(600.0, float(window_seconds)))
        self.min_samples = max(6, min(120, int(min_samples)))
        self.threshold = max(2.0, min(8.0, float(threshold)))
        self.min_features = max(2, min(5, int(min_features)))
        self.max_players = max(100, int(max_players))
        self.events: dict[str, deque[tuple[float, str, float]]] = defaultdict(deque)
        self.baselines: dict[str, dict[str, deque[float]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=60))
        )
        self.last_alert: dict[str, float] = {}

    def observe(self, player_id: str, feature: str, value: float, now: float | None = None) -> None:
        if not player_id or feature not in {
            "move_speed", "combat_hit", "combat_target", "ore", "rock",
            "click_interval", "scaffold_place",
        }:
            return
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(number):
            return
        timestamp = time.monotonic() if now is None else float(now)
        events = self.events[player_id]
        events.append((timestamp, feature, number))
        self._prune(events, timestamp - self.window_seconds)
        if len(self.events) > self.max_players:
            oldest = next(iter(self.events))
            self.events.pop(oldest, None)
            self.baselines.pop(oldest, None)
            self.last_alert.pop(oldest, None)

    @staticmethod
    def _prune(events: deque[tuple[float, str, float]], cutoff: float) -> None:
        while events and events[0][0] < cutoff:
            events.popleft()

    def _features(self, events: deque[tuple[float, str, float]], now: float) -> dict[str, float]:
        self._prune(events, now - self.window_seconds)
        values: dict[str, list[float]] = defaultdict(list)
        for _, key, value in events:
            values[key].append(value)
        duration = self.window_seconds
        speeds = values["move_speed"]
        intervals = values["click_interval"]
        click_cv = 0.0
        if len(intervals) >= 8:
            mean = statistics.fmean(intervals)
            if mean > 0:
                click_cv = statistics.pstdev(intervals) / mean
        ores, rocks = len(values["ore"]), len(values["rock"])
        total_mined = ores + rocks
        return {
            "move_speed_mean": statistics.fmean(speeds) if speeds else 0.0,
            "move_speed_peak": max(speeds, default=0.0),
            "combat_rate": len(values["combat_hit"]) / duration,
            "target_diversity": len(set(values["combat_target"])),
            "ore_ratio": ores / total_mined if total_mined else 0.0,
            "click_cv": click_cv,
            "scaffold_rate": len(values["scaffold_place"]) / duration,
        }

    @staticmethod
    def _robust_z(value: float, history: deque[float]) -> float:
        if len(history) < 6:
            return 0.0
        median = statistics.median(history)
        mad = statistics.median(abs(item - median) for item in history)
        scale = max(1e-6, 1.4826 * mad)
        # A small floor avoids exploding scores for naturally stable features.
        scale = max(scale, abs(median) * 0.05, 0.01)
        return abs(value - median) / scale

    def snapshot(self, player_id: str, now: float | None = None) -> dict[str, float] | None:
        """Return current model features without updating personal baselines."""
        timestamp = time.monotonic() if now is None else float(now)
        events = self.events.get(player_id)
        if not events:
            return None
        return dict(self._features(events, timestamp))

    def analyze(self, player_id: str, now: float | None = None) -> dict[str, Any] | None:
        timestamp = time.monotonic() if now is None else float(now)
        events = self.events.get(player_id)
        if not events:
            return None
        features = self._features(events, timestamp)
        baseline = self.baselines[player_id]
        anomalous: list[tuple[str, float, float]] = []
        for key, value in features.items():
            history = baseline[key]
            if len(history) >= self.min_samples:
                z_score = self._robust_z(value, history)
                if z_score >= self.threshold:
                    anomalous.append((key, value, z_score))
            # Do not immediately learn extreme observations; this limits baseline poisoning.
            if not anomalous or all(item[0] != key for item in anomalous):
                history.append(value)
        groups = {
            "move_speed_mean": "movement", "move_speed_peak": "movement",
            "combat_rate": "combat", "target_diversity": "combat",
            "ore_ratio": "mining", "click_cv": "input",
            "scaffold_rate": "building",
        }
        anomalous_groups = {groups.get(item[0], item[0]) for item in anomalous}
        if len(anomalous_groups) < self.min_features:
            return None
        previous = self.last_alert.get(player_id, 0.0)
        if timestamp - previous < 90.0:
            return None
        self.last_alert[player_id] = timestamp
        anomalous.sort(key=lambda item: item[2], reverse=True)
        return {
            "kind": "local_statistical_anomaly",
            "feature_count": len(anomalous),
            "independent_behavior_groups": len(anomalous_groups),
            "features": [
                {"name": key, "value": round(value, 4), "robust_z": round(z, 2)}
                for key, value, z in anomalous[:5]
            ],
            "window_seconds": self.window_seconds,
            "baseline_samples": min((len(v) for v in baseline.values()), default=0),
        }
