import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from plugins.endstone_zp_anticheat.client_security import (
    NativeClientRegistry, PROTOCOL, validate_telemetry,
)


class NativeClientSecurityTests(unittest.TestCase):
    def test_pair_code_is_single_use_and_token_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native_clients.json"
            registry = NativeClientRegistry(path)
            code = registry.create_pair_code("uuid-a", now=100)
            paired = registry.consume_pair_code(code, "android-test", now=101)
            self.assertIsNotNone(paired)
            uuid, token = paired
            self.assertEqual(uuid, "uuid-a")
            self.assertIsNone(registry.consume_pair_code(code, now=102))
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn(token, saved)
            self.assertIn(hashlib.sha256(token.encode()).hexdigest(), saved)

    def test_pair_code_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = NativeClientRegistry(Path(directory) / "clients.json")
            code = registry.create_pair_code("uuid-a", now=100)
            self.assertIsNone(registry.consume_pair_code(code, now=401))

    def test_tokens_are_bound_to_uuid_and_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = NativeClientRegistry(Path(directory) / "clients.json")
            code = registry.create_pair_code("uuid-a", now=100)
            uuid, token = registry.consume_pair_code(code, now=101)
            self.assertEqual(registry.resolve_token(token, now=102)[0], uuid)
            self.assertEqual(registry.revoke_player("uuid-a"), 1)
            self.assertIsNone(registry.resolve_token(token, now=103))

    def test_challenge_proof_is_session_bound(self):
        token = "test-token-with-sufficient-length-123456"
        proof = NativeClientRegistry.make_proof(token, "session-a", "nonce-a")
        self.assertEqual(proof, NativeClientRegistry.make_proof(token, "session-a", "nonce-a"))
        self.assertNotEqual(proof, NativeClientRegistry.make_proof(token, "session-b", "nonce-a"))

    def test_signed_payload_rejects_tampering(self):
        token = "test-token-with-sufficient-length-123456"
        payload = {"type": "telemetry", "seq": 1, "session_id": "session"}
        signature = hmac.new(
            token.encode(), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(NativeClientRegistry.verify_payload(token, {**payload, "signature": signature}, signature))
        changed = {**payload, "seq": 2, "signature": signature}
        self.assertFalse(NativeClientRegistry.verify_payload(token, changed, signature))

    def test_telemetry_schema_is_bounded(self):
        valid = {
            "platform": "android",
            "integrity": {"app_signature_valid": True, "tamper_detected": False},
            "runtime": {"fps": 60, "memory_mb": 512, "heartbeat_ms": 50},
        }
        self.assertIsNotNone(validate_telemetry(valid))
        invalid = {**valid, "runtime": {"fps": 9999}}
        self.assertIsNone(validate_telemetry(invalid))
        self.assertIsNone(validate_telemetry({"integrity": {"tamper_detected": "false"}}))


if __name__ == "__main__":
    unittest.main()
