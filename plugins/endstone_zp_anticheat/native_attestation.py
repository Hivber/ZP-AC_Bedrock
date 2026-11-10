from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import ssl
import threading
import time
import uuid as uuidlib
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


PROTOCOL_VERSION = "zp-ac-native-v1"
MAX_BODY_BYTES = 16 * 1024
KEY_ID_RE = re.compile(r"^[a-f0-9]{32}$")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_uuid(value: Any) -> str:
    return str(uuidlib.UUID(str(value)))


def _decode_signature(value: Any) -> bytes:
    raw = base64.b64decode(str(value or ""), validate=True)
    if not 8 <= len(raw) <= 144:
        raise ValueError("invalid signature size")
    return raw


def _load_p256_public_key(pem: str) -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:
        raise RuntimeError("cryptography is required for native client attestation") from exc
    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("only ECDSA P-256 public keys are accepted")
    return key


def _verify_signature(public_key_pem: str, message: bytes, signature_b64: str) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    key = _load_p256_public_key(public_key_pem)
    key.verify(_decode_signature(signature_b64), message, ec.ECDSA(hashes.SHA256()))


def _key_id_from_pem(pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = _load_p256_public_key(pem)
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:32]


class NativeAttestationService:
    """Optional native-client registration and one-time challenge verifier.

    Client claims are recorded as telemetry only. A valid signature proves possession
    of a registered private key, not that the game process is unmodified.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        challenge_ttl_seconds: float = 45.0,
        pairing_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
        result_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store_path = Path(store_path)
        self.challenge_ttl_seconds = max(10.0, float(challenge_ttl_seconds))
        self.pairing_ttl_seconds = max(60.0, float(pairing_ttl_seconds))
        self.clock = clock
        self.result_callback = result_callback
        self._lock = threading.RLock()
        self._online: set[str] = set()
        self._pair_codes: dict[str, dict[str, Any]] = {}
        self._challenges: dict[str, dict[str, Any]] = {}
        self._rates: dict[str, deque[float]] = defaultdict(deque)
        self._registry = self._load_registry()

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            result: dict[str, dict[str, Any]] = {}
            for player_uuid, record in raw.items():
                try:
                    key = _canonical_uuid(player_uuid)
                    if not isinstance(record, dict):
                        continue
                    key_id = str(record.get("key_id", ""))
                    pem = str(record.get("public_key_pem", ""))
                    if not KEY_ID_RE.fullmatch(key_id) or not pem:
                        continue
                    if _key_id_from_pem(pem) != key_id:
                        continue
                    result[key] = dict(record)
                except Exception:
                    continue
            return result
        except FileNotFoundError:
            return {}
        except Exception:
            backup = self.store_path.with_suffix(self.store_path.suffix + ".bak")
            try:
                raw = json.loads(backup.read_text(encoding="utf-8"))
                return raw if isinstance(raw, dict) else {}
            except Exception:
                return {}

    def _save_registry(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._registry, ensure_ascii=False, indent=2).encode("utf-8")
        temp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        backup = self.store_path.with_suffix(self.store_path.suffix + ".bak")
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if self.store_path.exists():
            try:
                backup.write_bytes(self.store_path.read_bytes())
            except OSError:
                pass
        os.replace(temp, self.store_path)

    def set_online(self, player_uuid: str, online: bool) -> None:
        key = _canonical_uuid(player_uuid)
        with self._lock:
            if online:
                self._online.add(key)
            else:
                self._online.discard(key)
                for challenge_id, challenge in list(self._challenges.items()):
                    if challenge["player_uuid"] == key:
                        self._challenges.pop(challenge_id, None)

    def create_pairing_code(self, player_uuid: str) -> str:
        key = _canonical_uuid(player_uuid)
        code = secrets.token_urlsafe(24)
        now = self.clock()
        with self._lock:
            self._pair_codes[key] = {
                "code_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "expires_at": now + self.pairing_ttl_seconds,
            }
        return code

    def enroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        player_uuid = _canonical_uuid(payload.get("player_uuid"))
        key_id = str(payload.get("key_id", ""))
        pem = str(payload.get("public_key_pem", ""))
        code = str(payload.get("pairing_code", ""))
        if not KEY_ID_RE.fullmatch(key_id) or len(pem) > 4096 or not pem.startswith("-----BEGIN PUBLIC KEY-----"):
            raise ValueError("invalid client public key")
        if _key_id_from_pem(pem) != key_id:
            raise ValueError("key id does not match public key")
        message = f"zp-ac-enroll-v1|{player_uuid}|{key_id}|{code}".encode("utf-8")
        try:
            _verify_signature(pem, message, str(payload.get("proof", "")))
        except Exception as exc:
            raise ValueError("invalid enrollment proof") from exc
        now = self.clock()
        with self._lock:
            if player_uuid not in self._online:
                raise ValueError("player must be online to enroll")
            pending = self._pair_codes.get(player_uuid)
            if not pending or pending["expires_at"] < now:
                self._pair_codes.pop(player_uuid, None)
                raise ValueError("pairing code expired or missing")
            supplied_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(supplied_hash, pending["code_hash"]):
                raise ValueError("pairing code mismatch")
            self._pair_codes.pop(player_uuid, None)
            self._challenges = {
                cid: challenge for cid, challenge in self._challenges.items()
                if challenge["player_uuid"] != player_uuid
            }
            self._registry[player_uuid] = {
                "key_id": key_id,
                "public_key_pem": pem,
                "registered_at": now,
                "last_sequence": 0,
                "last_attestation_at": 0.0,
                "client_version": "",
                "build_id": "",
                "capabilities": [],
                "integrity_claims": {},
                "signature_verified": True,
            }
            self._save_registry()
        return {"ok": True, "protocol": PROTOCOL_VERSION, "player_uuid": player_uuid, "key_id": key_id}

    def issue_challenge(self, player_uuid: str, key_id: str) -> dict[str, Any]:
        key = _canonical_uuid(player_uuid)
        now = self.clock()
        with self._lock:
            record = self._registry.get(key)
            if key not in self._online:
                raise ValueError("player is not online")
            if not record or not secrets.compare_digest(str(record.get("key_id", "")), str(key_id)):
                raise ValueError("native client is not paired")
            for stale_id, stale in list(self._challenges.items()):
                if stale["expires_at"] <= now:
                    self._challenges.pop(stale_id, None)
            if len(self._challenges) >= 4096:
                raise ValueError("too many pending challenges")
            sequence = int(record.get("last_sequence", 0)) + 1
            challenge_id = secrets.token_urlsafe(18)
            nonce = secrets.token_urlsafe(32)
            self._challenges[challenge_id] = {
                "player_uuid": key,
                "key_id": key_id,
                "nonce": nonce,
                "sequence": sequence,
                "issued_at": now,
                "expires_at": now + self.challenge_ttl_seconds,
            }
            return {
                "protocol": PROTOCOL_VERSION,
                "challenge_id": challenge_id,
                "nonce": nonce,
                "sequence": sequence,
                "expires_in": int(self.challenge_ttl_seconds),
            }

    def verify_attestation(self, payload: dict[str, Any]) -> dict[str, Any]:
        player_uuid = _canonical_uuid(payload.get("player_uuid"))
        key_id = str(payload.get("key_id", ""))
        challenge_id = str(payload.get("challenge_id", ""))
        nonce = str(payload.get("nonce", ""))
        signature = str(payload.get("signature", ""))
        now = self.clock()
        with self._lock:
            challenge = self._challenges.pop(challenge_id, None)
            record = self._registry.get(player_uuid)
            if player_uuid not in self._online:
                raise ValueError("player is not online")
            if not challenge or challenge["expires_at"] <= now:
                raise ValueError("challenge expired or already used")
            if challenge["player_uuid"] != player_uuid or challenge["key_id"] != key_id or challenge["nonce"] != nonce:
                raise ValueError("challenge binding mismatch")
            if not record or record.get("key_id") != key_id:
                raise ValueError("native client key is not registered")
            sequence = int(payload.get("sequence", -1))
            if type(payload.get("sequence")) is not int or sequence != challenge["sequence"] or sequence <= int(record.get("last_sequence", 0)):
                raise ValueError("invalid or replayed sequence")
            if payload.get("protocol") != PROTOCOL_VERSION:
                raise ValueError("protocol mismatch")
            signed_payload = {k: v for k, v in payload.items() if k != "signature"}
            if len(canonical_json(signed_payload)) > MAX_BODY_BYTES:
                raise ValueError("attestation payload too large")
            try:
                _verify_signature(str(record["public_key_pem"]), canonical_json(signed_payload), signature)
            except Exception as exc:
                raise ValueError("invalid attestation signature") from exc
            capabilities = payload.get("capabilities", [])
            integrity = payload.get("integrity", {})
            if not isinstance(capabilities, list) or len(capabilities) > 32:
                raise ValueError("invalid capabilities")
            if not isinstance(integrity, dict) or len(integrity) > 24:
                raise ValueError("invalid integrity claims")
            safe_capabilities = [str(x)[:48] for x in capabilities if isinstance(x, str)][:32]
            safe_integrity = {
                str(k)[:48]: bool(v) for k, v in integrity.items()
                if isinstance(k, str) and isinstance(v, bool)
            }
            telemetry = payload.get("telemetry", {})
            if not isinstance(telemetry, dict) or len(telemetry) > 32:
                raise ValueError("invalid telemetry")
            safe_telemetry: dict[str, Any] = {}
            for key, value in telemetry.items():
                if not isinstance(key, str):
                    continue
                if isinstance(value, bool) or isinstance(value, int):
                    safe_telemetry[key[:48]] = value
                elif isinstance(value, float) and math.isfinite(value):
                    safe_telemetry[key[:48]] = value
                elif isinstance(value, str):
                    safe_telemetry[key[:48]] = value[:96]
            result = {
                "player_uuid": player_uuid,
                "key_id": key_id,
                "sequence": sequence,
                "client_version": str(payload.get("client_version", ""))[:48],
                "build_id": str(payload.get("build_id", ""))[:96],
                "capabilities": safe_capabilities,
                "integrity_claims": safe_integrity,
                "telemetry": safe_telemetry,
                "signature_verified": True,
                "received_at": now,
            }
            record.update({
                "last_sequence": sequence,
                "last_attestation_at": now,
                "client_version": result["client_version"],
                "build_id": result["build_id"],
                "capabilities": safe_capabilities,
                "integrity_claims": safe_integrity,
                "telemetry": safe_telemetry,
                "signature_verified": True,
            })
            self._save_registry()
        if self.result_callback:
            self.result_callback(dict(result))
        return {"ok": True, "verified": True, "sequence": sequence}

    def revoke(self, player_uuid: str) -> bool:
        key = _canonical_uuid(player_uuid)
        with self._lock:
            removed = self._registry.pop(key, None) is not None
            self._pair_codes.pop(key, None)
            for cid, challenge in list(self._challenges.items()):
                if challenge["player_uuid"] == key:
                    self._challenges.pop(cid, None)
            if removed:
                self._save_registry()
            return removed

    def status(self, player_uuid: str) -> dict[str, Any]:
        key = _canonical_uuid(player_uuid)
        with self._lock:
            record = self._registry.get(key)
            if not record:
                return {"paired": False, "online": key in self._online}
            return {
                "paired": True,
                "online": key in self._online,
                "key_id": record.get("key_id", ""),
                "client_version": record.get("client_version", ""),
                "build_id": record.get("build_id", ""),
                "last_attestation_at": float(record.get("last_attestation_at", 0.0)),
                "last_sequence": int(record.get("last_sequence", 0)),
                "capabilities": list(record.get("capabilities", [])),
                "integrity_claims": dict(record.get("integrity_claims", {})),
                "telemetry": dict(record.get("telemetry", {})),
                "signature_verified": bool(record.get("signature_verified", False)),
            }

    def allow_request(self, remote: str, *, limit: int = 60, window: float = 60.0) -> bool:
        now = self.clock()
        with self._lock:
            bucket = self._rates[remote]
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            if len(self._rates) > 2048:
                oldest = min(self._rates, key=lambda key: self._rates[key][0] if self._rates[key] else 0.0)
                if oldest != remote:
                    self._rates.pop(oldest, None)
            return True


class NativeAttestationHTTPServer:
    def __init__(
        self,
        service: NativeAttestationService,
        host: str,
        port: int,
        certfile: str = "",
        keyfile: str = "",
    ) -> None:
        self.service = service
        self.host = str(host)
        self.port = int(port)
        self.certfile = str(certfile or "")
        self.keyfile = str(keyfile or "")
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if bool(self.certfile) != bool(self.keyfile):
            raise ValueError("both TLS certificate and key paths are required")
        if self.host not in LOOPBACK_HOSTS and not (self.certfile and self.keyfile):
            raise ValueError("non-loopback native attestation listener requires TLS certificate and key; use a TLS reverse proxy otherwise")
        service = self.service

        class Handler(BaseHTTPRequestHandler):
            server_version = "ZP-AC-Native/1"
            sys_version = ""

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if urlsplit(self.path).path != "/v1/health":
                    self._reply(404, {"ok": False, "error": "not_found"})
                    return
                self._reply(200, {"ok": True, "protocol": PROTOCOL_VERSION})

            def do_POST(self) -> None:
                remote = self.client_address[0] if self.client_address else "unknown"
                if not service.allow_request(remote):
                    self._reply(429, {"ok": False, "error": "rate_limited"})
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self._reply(415, {"ok": False, "error": "json_required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._reply(400, {"ok": False, "error": "invalid_length"})
                    return
                if length < 2 or length > MAX_BODY_BYTES:
                    self._reply(413, {"ok": False, "error": "invalid_body_size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("object_required")
                    path = urlsplit(self.path).path
                    if path == "/v1/enroll":
                        result = service.enroll(payload)
                    elif path == "/v1/challenge":
                        result = service.issue_challenge(payload.get("player_uuid"), str(payload.get("key_id", "")))
                    elif path == "/v1/attest":
                        result = service.verify_attestation(payload)
                    else:
                        self._reply(404, {"ok": False, "error": "not_found"})
                        return
                    self._reply(200, result)
                except (ValueError, TypeError, KeyError):
                    self._reply(400, {"ok": False, "error": "request_rejected"})
                except RuntimeError:
                    self._reply(503, {"ok": False, "error": "crypto_unavailable"})
                except Exception:
                    self._reply(500, {"ok": False, "error": "internal_error"})

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        try:
            if self.certfile and self.keyfile:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(self.certfile, self.keyfile)
                self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
            self.thread = threading.Thread(target=self.httpd.serve_forever, name="ZPAC-NativeAttestation", daemon=True)
            self.thread.start()
        except Exception:
            self.httpd.server_close()
            self.httpd = None
            self.thread = None
            raise

    def stop(self) -> None:
        server = self.httpd
        self.httpd = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread = self.thread
        self.thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
