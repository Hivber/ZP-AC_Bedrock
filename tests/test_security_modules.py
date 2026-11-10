import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


EVIDENCE = load_module("zp_evidence_store", "plugins/endstone_zp_anticheat/evidence_store.py")
METRICS = load_module("zp_runtime_metrics", "plugins/endstone_zp_anticheat/runtime_metrics.py")
MINING = load_module("zp_mining_analysis", "plugins/endstone_zp_anticheat/mining_analysis.py")


class EvidenceStoreTests(unittest.TestCase):
    def test_append_recent_and_review_are_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EVIDENCE.EvidenceStore(root / "evidence.jsonl", root / "reviews.json", max_records=100)
            row = store.append({"target_uuid": "uuid-a", "code": "speed_sustained", "detail": "sample"})
            self.assertEqual(len(row["evidence_id"]), 16)
            self.assertEqual(store.recent("uuid-a", 5)[0]["code"], "speed_sustained")
            review = store.review(row["evidence_id"], "dismissed", "admin", "false positive")
            self.assertEqual(review["status"], "dismissed")
            reopened = EVIDENCE.EvidenceStore(root / "evidence.jsonl", root / "reviews.json")
            self.assertEqual(reopened.recent("uuid-a")[0]["review"]["status"], "dismissed")

    def test_store_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EVIDENCE.EvidenceStore(root / "evidence.jsonl", root / "reviews.json", max_records=100)
            for index in range(125):
                store.append({"target_uuid": "uuid-a", "index": index})
            self.assertEqual(store.count(), 100)
            self.assertEqual(store.recent("uuid-a", 1)[0]["index"], 124)

    def test_review_rejects_unknown_and_invalid_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EVIDENCE.EvidenceStore(root / "evidence.jsonl", root / "reviews.json")
            with self.assertRaises(ValueError):
                store.review("bad", "confirmed", "admin")
            with self.assertRaises(KeyError):
                store.review("0123456789abcdef", "confirmed", "admin")


class RuntimeMetricsTests(unittest.TestCase):
    def test_counters_and_latency_percentiles(self):
        metrics = METRICS.RuntimeMetrics()
        metrics.increment("block_break")
        metrics.increment("block_break", 2)
        metrics.observe_ms("pulse", 2.0)
        metrics.observe_ms("pulse", 8.0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["block_break"], 3)
        self.assertEqual(snapshot["durations"]["pulse"]["max_ms"], 8.0)
        self.assertEqual(snapshot["durations"]["pulse"]["samples"], 2)


class MiningAnalysisTests(unittest.TestCase):
    def test_block_classification(self):
        self.assertEqual(MINING.classify_mined_block("minecraft:deepslate_diamond_ore"), "ore")
        self.assertEqual(MINING.classify_mined_block("minecraft:deepslate"), "rock")
        self.assertIsNone(MINING.classify_mined_block("minecraft:oak_log"))

    def test_summary_counts_rare_ores_and_clusters(self):
        rows = [
            (1, "overworld", 0, 10, 0, "diamond_ore", "ore"),
            (2, "overworld", 20, 10, 0, "emerald_ore", "ore"),
            (3, "overworld", 1, 10, 0, "stone", "rock"),
        ]
        result = MINING.summarize_mining_events(rows, 5.5)
        self.assertEqual(result["ore_count"], 2)
        self.assertEqual(result["rare_count"], 2)
        self.assertEqual(result["cluster_count"], 2)
        self.assertEqual(result["rock_count"], 1)


if __name__ == "__main__":
    unittest.main()
