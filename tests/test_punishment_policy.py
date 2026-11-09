import importlib.util
import unittest
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[1] / "plugins" / "endstone_zp_anticheat" / "policy.py"
SPEC = importlib.util.spec_from_file_location("zp_ac_punishment_policy", POLICY_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

independent_strong_domain_count = MODULE.independent_strong_domain_count
has_independent_strong_corroboration = MODULE.has_independent_strong_corroboration


class PunishmentPolicyTests(unittest.TestCase):
    def test_speed_plus_weak_scaffold_is_not_independent_strong_corroboration(self):
        self.assertEqual(independent_strong_domain_count({"speed_sustained"}), 1)
        self.assertFalse(has_independent_strong_corroboration({"speed_sustained"}))
        self.assertFalse(has_independent_strong_corroboration({"speed_sustained", "scaffold_pattern"}))

    def test_two_movement_rules_are_one_domain(self):
        self.assertEqual(independent_strong_domain_count({"speed_sustained", "hover_confirmed"}), 1)
        self.assertFalse(has_independent_strong_corroboration({"speed_sustained", "hover_confirmed"}))

    def test_movement_and_combat_strong_rules_are_independent(self):
        codes = {"speed_sustained", "reach_extreme"}
        self.assertEqual(independent_strong_domain_count(codes), 2)
        self.assertTrue(has_independent_strong_corroboration(codes))

    def test_two_combat_rules_are_one_domain(self):
        self.assertEqual(independent_strong_domain_count({"reach_extreme", "multi_target_combat"}), 1)
        self.assertFalse(has_independent_strong_corroboration({"reach_extreme", "multi_target_combat"}))

    def test_unknown_and_weak_rules_do_not_count(self):
        self.assertEqual(independent_strong_domain_count({"scaffold_pattern", "autoclick_pattern", "unknown"}), 0)


if __name__ == "__main__":
    unittest.main()
