import importlib.util
import unittest
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[1] / "plugins" / "endstone_zp_anticheat" / "policy.py"
SPEC = importlib.util.spec_from_file_location("zp_ac_policy", POLICY_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FalsePositivePolicyTests(unittest.TestCase):
    def test_speed_plus_weak_scaffold_must_not_auto_kick(self):
        self.assertFalse(MODULE.has_independent_strong_evidence(
            {"speed_sustained", "scaffold_pattern"},
            {"speed_sustained"},
            2,
        ))

    def test_two_distinct_strong_rule_families_can_pass_gate(self):
        self.assertTrue(MODULE.has_independent_strong_evidence(
            {"speed_sustained", "hover_confirmed", "scaffold_pattern"},
            {"speed_sustained", "hover_confirmed"},
            2,
        ))

    def test_repeated_same_rule_is_not_independent_evidence(self):
        self.assertFalse(MODULE.has_independent_strong_evidence(
            {"speed_sustained"},
            {"speed_sustained"},
            2,
        ))

    def test_unknown_or_unlinked_strong_code_is_ignored(self):
        self.assertFalse(MODULE.has_independent_strong_evidence(
            {"speed_sustained", "scaffold_pattern"},
            {"speed_sustained", "reach_extreme"},
            2,
        ))

    def test_weak_evidence_does_not_increase_persistent_risk_or_reduce_trust(self):
        self.assertEqual(MODULE.profile_penalty(4, 3, False), (0.0, 0.0))

    def test_high_confidence_evidence_still_affects_profile(self):
        risk, trust = MODULE.profile_penalty(4, 1, True)
        self.assertGreater(risk, 0.0)
        self.assertGreater(trust, 0.0)


if __name__ == "__main__":
    unittest.main()
