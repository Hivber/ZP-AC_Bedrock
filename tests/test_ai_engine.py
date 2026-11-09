import importlib.util
import unittest
from pathlib import Path

ENGINE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "endstone_zp_anticheat" / "ai" / "engine.py"
SPEC = importlib.util.spec_from_file_location("zp_ac_local_ai_engine", ENGINE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
LocalBehaviorAI = MODULE.LocalBehaviorAI


class LocalBehaviorAITests(unittest.TestCase):
    def test_snapshot_exposes_seven_features(self):
        engine = LocalBehaviorAI()
        engine.observe('p', 'move_speed', 2.0, 1000.0)
        snapshot = engine.snapshot('p', 1000.0)
        self.assertEqual(len(snapshot), 7)
        self.assertEqual(snapshot['move_speed_mean'], 2.0)

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
        self.assertGreaterEqual(result["independent_behavior_groups"], 2)
        self.assertEqual(result["kind"], "local_statistical_anomaly")

    def test_one_behavior_domain_alone_does_not_flag(self):
        engine = LocalBehaviorAI(window_seconds=60, min_samples=6, threshold=3.0, min_features=2)
        player = "00000000-0000-0000-0000-000000000003"
        for index in range(8):
            now = 2000.0 + index * 5.0
            engine.observe(player, "move_speed", 1.0, now)
            engine.analyze(player, now)
        now = 2045.0
        engine.observe(player, "move_speed", 12.0, now)
        self.assertIsNone(engine.analyze(player, now))

    def test_non_finite_and_unknown_features_are_ignored(self):
        engine = LocalBehaviorAI()
        player = "player"
        engine.observe(player, "move_speed", float("nan"), 1000.0)
        engine.observe(player, "unknown", 1.0, 1000.0)
        self.assertNotIn(player, engine.events)


if __name__ == "__main__":
    unittest.main()
