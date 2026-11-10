import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zp_chat_moderation", ROOT / "plugins/endstone_zp_anticheat/chat_moderation.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)
RecentChatStore = MOD.RecentChatStore
detect_insult_terms = MOD.detect_insult_terms


class ChatModerationTests(unittest.TestCase):
    def test_detects_common_chinese_insults(self):
        self.assertIn("傻逼", detect_insult_terms("你真是个傻逼"))
        self.assertIn("废物", detect_insult_terms("你就是废物"))

    def test_normalizes_punctuation_spacing_and_width(self):
        self.assertIn("傻逼", detect_insult_terms("你是 傻．逼！！"))
        self.assertIn("傻逼", detect_insult_terms("你是傻 逼"))

    def test_does_not_match_unrelated_chat(self):
        self.assertEqual(detect_insult_terms("大家晚上好，今天一起玩吧"), ())
        self.assertEqual(detect_insult_terms("这个垃圾桶放在哪里"), ())
        self.assertEqual(detect_insult_terms("USB设备接口"), ())

    def test_history_is_separated_by_player(self):
        store = RecentChatStore(window_seconds=600, max_messages_per_player=5)
        store.record("uuid-a", "普通聊天", now=100)
        store.record("uuid-b", "你是傻逼", now=101)
        self.assertEqual(store.find_insults("uuid-a", now=102), [])
        self.assertEqual(len(store.find_insults("uuid-b", now=102)), 1)

    def test_history_expires_after_window(self):
        store = RecentChatStore(window_seconds=60, max_messages_per_player=5)
        store.record("uuid-a", "你是傻逼", now=100)
        self.assertEqual(store.find_insults("uuid-a", now=159), [])
        self.assertEqual(store.find_insults("uuid-a", now=160), [])

    def test_history_is_bounded(self):
        store = RecentChatStore(window_seconds=600, max_messages_per_player=2)
        store.record("uuid-a", "第一条", now=100)
        store.record("uuid-a", "第二条", now=101)
        store.record("uuid-a", "第三条", now=102)
        self.assertEqual([item["message"] for item in store.recent("uuid-a", now=103)], ["第二条", "第三条"])


if __name__ == "__main__":
    unittest.main()
