import importlib.util
import unittest
from pathlib import Path

CREDIT_PATH = Path(__file__).resolve().parents[1] / "plugins" / "endstone_zp_anticheat" / "credit.py"
SPEC = importlib.util.spec_from_file_location("zp_ac_public_credit", CREDIT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class PublicCreditPolicyTests(unittest.TestCase):
    def test_new_player_starts_at_full_credit(self):
        self.assertEqual(MODULE.PUBLIC_CREDIT_DEFAULT, 100.0)

    def test_score_is_clamped(self):
        self.assertEqual(MODULE.clamp_public_credit(-10), 0.0)
        self.assertEqual(MODULE.clamp_public_credit(140), 100.0)
        self.assertEqual(MODULE.clamp_public_credit("invalid"), 100.0)

    def test_only_scores_below_70_are_locked(self):
        self.assertTrue(MODULE.can_damage_players(70.0, 70.0))
        self.assertTrue(MODULE.can_damage_players(100.0, 70.0))
        self.assertFalse(MODULE.can_damage_players(69.99, 70.0))

    def test_explicit_bypass_is_supported(self):
        self.assertTrue(MODULE.can_damage_players(0, 70.0, exempt=True))

    def test_recovery_requires_delay_and_active_time(self):
        self.assertEqual(MODULE.recover_public_credit(60, 3600, 0.5, 1200, 3600), 60.0)
        self.assertEqual(MODULE.recover_public_credit(60, 3600, 0.5, 7200, 3600), 60.5)

    def test_recovery_caps_at_100(self):
        self.assertEqual(MODULE.recover_public_credit(99.9, 3600, 5.0, 7200, 3600), 100.0)

    def test_penalty_never_goes_below_zero(self):
        self.assertEqual(MODULE.apply_public_credit_penalty(4, 12), 0.0)
        self.assertEqual(MODULE.apply_public_credit_penalty(100, 12), 88.0)


if __name__ == "__main__":
    unittest.main()
