import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "endstone_zp_anticheat" / "movement.py"
SPEC = importlib.util.spec_from_file_location("zp_ac_movement", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeEffect:
    def __init__(self, amplifier):
        self.amplifier = amplifier


class FakePlayer:
    def __init__(self, effects=None, fail=False):
        self.effects = effects or {}
        self.fail = fail

    def get_effect(self, effect_id):
        if self.fail:
            raise RuntimeError("effect API unavailable")
        return self.effects.get(effect_id)


class MovementEffectTests(unittest.TestCase):
    def test_absent_effect_returns_minus_one(self):
        self.assertEqual(MODULE.read_effect_amplifier(FakePlayer(), "speed"), -1)

    def test_active_effect_returns_amplifier(self):
        player = FakePlayer({"speed": FakeEffect(2)})
        self.assertEqual(MODULE.read_effect_amplifier(player, "speed"), 2)

    def test_unreadable_effect_fails_safe(self):
        self.assertIsNone(MODULE.read_effect_amplifier(FakePlayer(fail=True), "speed"))

    def test_speed_multiplier_tracks_effect_level(self):
        self.assertEqual(MODULE.speed_effect_multiplier(-1), 1.0)
        self.assertAlmostEqual(MODULE.speed_effect_multiplier(0), 1.2)
        self.assertAlmostEqual(MODULE.speed_effect_multiplier(1), 1.4)
        self.assertAlmostEqual(MODULE.speed_effect_multiplier(2), 1.6)

    def test_extreme_amplifier_is_bounded(self):
        self.assertLessEqual(MODULE.speed_effect_multiplier(255), 3.0)


if __name__ == "__main__":
    unittest.main()
