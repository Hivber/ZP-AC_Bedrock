import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from endstone_zp_anticheat.ai.engine import LocalBehaviorAI


class LocalBehaviorAITests(unittest.TestCase):
    def test_requires_personal_baseline_before_flagging(self):
        engine = LocalBehaviorAI(window_seconds=60, min_samples=6, threshold=3.0)
        player = "00000000-0000-0000-0000-000000000001"
        for index in range(8):
            now = 1000.0 + index * 5.0
            engine.observe(player, "move_speed", 1.0, now)
            self.assertIsNone(engine.analyze(player, now))
        self.assertIsNone(engine.analyze("unknown-player", 1050.0))

    def test_multifeature_deviation_creates_review_finding(self):
        engine = LocalBehaviorAI(window_seconds=60, min_samples=6, threshold=3.0, min_features=2)
        player = "00000000-0000-0000-0000-000000000002"
        for index in range(8):
            now = 1000.0 + index * 5.0
            engine.observe(player, "move_speed", 1.0, now)
            engine.analyze(player, now)
        now = 1045.0
        engine.observe(player, "move_speed", 12.0, now)
        engine.observe(player, "combat_hit", 1.0, now)
        engine.observe(player, "combat_target", 12345.0, now)
        result = engine.analyze(player, now)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["feature_count"], 2)
        self.assertEqual(result["kind"], "local_statistical_anomaly")

    def test_input_is_bounded_and_non_finite_values_are_ignored(self):
        engine = LocalBehaviorAI()
        player = "player"
        engine.observe(player, "move_speed", float("nan"), 1000.0)
        engine.observe(player, "unknown", 1.0, 1000.0)
        self.assertNotIn(player, engine.events)


if __name__ == "__main__":
    unittest.main()
