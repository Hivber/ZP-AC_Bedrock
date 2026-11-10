import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zp_mute_store", ROOT / "plugins/endstone_zp_anticheat/mute.py"
)
MUTE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MUTE)
MuteStore = MUTE.MuteStore
duration_for_violation = MUTE.duration_for_violation


class MuteStoreTests(unittest.TestCase):
    def test_escalating_durations_and_cap(self):
        expected = [600, 1800, 3600, 10800, 21600, 43200, 86400, 86400]
        self.assertEqual(
            [duration_for_violation(index) for index in range(1, 9)],
            expected,
        )

    def test_first_report_mutes_immediately_and_increases_on_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MuteStore(Path(directory) / "muted.json")
            first = store.record_violation("uuid-a", now=1000)
            self.assertEqual(first["violation_count"], 1)
            self.assertEqual(first["duration_seconds"], 600)
            self.assertTrue(store.is_muted("uuid-a", now=1001))

            second = store.record_violation("uuid-a", now=1100)
            self.assertEqual(second["violation_count"], 2)
            self.assertEqual(second["duration_seconds"], 1800)
            self.assertEqual(second["muted_until"], 2900)

    def test_mute_and_violation_count_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "muted.json"
            MuteStore(path).record_violation("uuid-a", now=1000)
            reopened = MuteStore(path)
            self.assertEqual(reopened.get("uuid-a")["violation_count"], 1)
            self.assertTrue(reopened.is_muted("uuid-a", now=1001))

    def test_expiry_and_admin_unmute(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MuteStore(Path(directory) / "muted.json")
            store.record_violation("uuid-a", now=1000)
            self.assertFalse(store.is_muted("uuid-a", now=1600))
            store.record_violation("uuid-a", now=2000)
            self.assertTrue(store.unmute("uuid-a"))
            self.assertFalse(store.is_muted("uuid-a", now=2001))
            self.assertEqual(store.get("uuid-a")["violation_count"], 2)


if __name__ == "__main__":
    unittest.main()
