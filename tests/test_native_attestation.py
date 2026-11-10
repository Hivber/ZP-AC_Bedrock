import base64
import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    hashes = serialization = ec = None

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zp_native_attestation", ROOT / "plugins/endstone_zp_anticheat/native_attestation.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


@unittest.skipIf(ec is None, "cryptography dependency is not installed")
class NativeAttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "native_clients.json"
        self.now = [1000.0]
        self.results = []
        self.service = MOD.NativeAttestationService(
            self.path,
            challenge_ttl_seconds=45,
            pairing_ttl_seconds=300,
            clock=lambda: self.now[0],
            result_callback=self.results.append,
        )
        self.player_uuid = str(uuid.uuid4())
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()
        self.public_pem = self.public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        self.key_id = MOD._key_id_from_pem(self.public_pem)
        self.service.set_online(self.player_uuid, True)

    def tearDown(self):
        self.temp.cleanup()

    def _sign(self, message):
        signature = self.private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode("ascii")

    def _enroll(self):
        code = self.service.create_pairing_code(self.player_uuid)
        message = f"zp-ac-enroll-v1|{self.player_uuid}|{self.key_id}|{code}".encode()
        result = self.service.enroll({
            "player_uuid": self.player_uuid,
            "key_id": self.key_id,
            "public_key_pem": self.public_pem,
            "pairing_code": code,
            "proof": self._sign(message),
        })
        self.assertTrue(result["ok"])

    def _attestation_payload(self):
        challenge = self.service.issue_challenge(self.player_uuid, self.key_id)
        payload = {
            "protocol": MOD.PROTOCOL_VERSION,
            "player_uuid": self.player_uuid,
            "key_id": self.key_id,
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "sequence": challenge["sequence"],
            "client_version": "test-client/1",
            "build_id": "test-build",
            "capabilities": ["input-telemetry", "keystore-key"],
            "integrity": {"package_signature_valid": True, "debugger_detected": False},
        }
        signed_bytes = __import__("json").dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return {
            "signed_payload": base64.b64encode(signed_bytes).decode("ascii"),
            "signature": self._sign(signed_bytes),
        }

    def test_pairing_requires_online_player_and_one_time_code(self):
        code = self.service.create_pairing_code(self.player_uuid)
        message = f"zp-ac-enroll-v1|{self.player_uuid}|{self.key_id}|{code}".encode()
        payload = {
            "player_uuid": self.player_uuid,
            "key_id": self.key_id,
            "public_key_pem": self.public_pem,
            "pairing_code": code,
            "proof": self._sign(message),
        }
        self.assertTrue(self.service.enroll(payload)["ok"])
        with self.assertRaises(ValueError):
            self.service.enroll(payload)

    def test_invalid_pairing_proof_is_rejected(self):
        code = self.service.create_pairing_code(self.player_uuid)
        with self.assertRaises(Exception):
            self.service.enroll({
                "player_uuid": self.player_uuid,
                "key_id": self.key_id,
                "public_key_pem": self.public_pem,
                "pairing_code": code,
                "proof": base64.b64encode(b"invalid").decode(),
            })

    def test_attestation_signature_verifies_and_is_recorded(self):
        self._enroll()
        result = self.service.verify_attestation(self._attestation_payload())
        self.assertTrue(result["verified"])
        status = self.service.status(self.player_uuid)
        self.assertTrue(status["signature_verified"])
        self.assertEqual(status["last_sequence"], 1)
        self.assertEqual(status["client_version"], "test-client/1")
        self.assertEqual(len(self.results), 1)

    def test_challenge_is_one_time_and_replay_is_rejected(self):
        self._enroll()
        payload = self._attestation_payload()
        self.assertTrue(self.service.verify_attestation(payload)["verified"])
        with self.assertRaises(ValueError):
            self.service.verify_attestation(payload)

    def test_expired_challenge_is_rejected(self):
        self._enroll()
        payload = self._attestation_payload()
        self.now[0] += 46
        with self.assertRaises(ValueError):
            self.service.verify_attestation(payload)

    def test_player_must_be_online_for_challenge_and_attestation(self):
        self._enroll()
        self.service.set_online(self.player_uuid, False)
        with self.assertRaises(ValueError):
            self.service.issue_challenge(self.player_uuid, self.key_id)

    def test_bad_signature_is_rejected(self):
        self._enroll()
        payload = self._attestation_payload()
        raw = base64.b64decode(payload["signed_payload"])
        tampered = raw.replace(b"test-build", b"fake-build")
        payload["signed_payload"] = base64.b64encode(tampered).decode("ascii")
        with self.assertRaises(ValueError):
            self.service.verify_attestation(payload)

    def test_revocation_removes_registered_key(self):
        self._enroll()
        self.assertTrue(self.service.revoke(self.player_uuid))
        self.assertFalse(self.service.status(self.player_uuid)["paired"])

    def test_registry_survives_restart(self):
        self._enroll()
        reopened = MOD.NativeAttestationService(self.path)
        self.assertTrue(reopened.status(self.player_uuid)["paired"])

    def test_ip_rate_limit_is_bounded(self):
        for _ in range(60):
            self.assertTrue(self.service.allow_request("192.0.2.1"))
        self.assertFalse(self.service.allow_request("192.0.2.1"))

    def test_non_loopback_listener_requires_tls(self):
        listener = MOD.NativeAttestationHTTPServer(self.service, "0.0.0.0", 0)
        with self.assertRaises(ValueError):
            listener.start()
        self.assertIsNone(listener.httpd)


if __name__ == "__main__":
    unittest.main()
