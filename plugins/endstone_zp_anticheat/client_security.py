from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any


PROTOCOL = "zp-ac-native-v1"


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class NativeClientRegistry:
    """Stores only token hashes and UUIDs; raw pairing tokens never touch disk."""

    def __init__(self, path: Path, max_tokens_per_player: int = 3) -> None:
        self.path = Path(path)
        self.max_tokens_per_player = max(1, min(10, int(max_tokens_per_player)))
        self._lock = threading.RLock()
        self._pair_codes: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            clients = payload.get("clients", {}) if isinstance(payload, dict) else {}
            if isinstance(clients, dict):
                for digest, record in clients.items():
                    if (
                        isinstance(digest, str) and len(digest) == 64
                        and isinstance(record, dict)
                        and isinstance(record.get("uuid"), str)
                    ):
                        self._clients[digest] = {
                            "uuid": record["uuid"][:128],
                            "client_id": str(record.get("client_id", ""))[:64],
                            "created_at": float(record.get("created_at", 0)),
                            "last_seen": float(record.get("last_seen", 0)),
                        }
        except FileNotFoundError:
            return
        except Exception:
            # Keep a corrupt file for operator inspection; never silently overwrite it.
            corrupt = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                if not corrupt.exists():
                    self.path.replace(corrupt)
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        data = {"version": 1, "clients": self._clients}
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.path)

    def create_pair_code(self, player_uuid: str, now: float | None = None) -> str:
        current = time.time() if now is None else float(now)
        key = str(player_uuid or "").strip()
        if not key:
            raise ValueError("player UUID is required")
        with self._lock:
            self._prune_codes(current)
            for _ in range(20):
                code = f"{secrets.randbelow(100_000_000):08d}"
                if code not in self._pair_codes:
                    self._pair_codes[code] = {"uuid": key, "expires_at": current + 300}
                    return code
        raise RuntimeError("could not allocate a pairing code")

    def _prune_codes(self, now: float) -> None:
        self._pair_codes = {
            code: item for code, item in self._pair_codes.items()
            if float(item.get("expires_at", 0)) > now
        }

    def consume_pair_code(
        self, code: str, client_id: str = "", now: float | None = None
    ) -> tuple[str, str] | None:
        current = time.time() if now is None else float(now)
        with self._lock:
            self._prune_codes(current)
            item = self._pair_codes.pop(str(code or "").strip(), None)
            if item is None:
                return None
            player_uuid = str(item["uuid"])
            token = secrets.token_urlsafe(32)
            digest = _token_hash(token)
            player_tokens = [
                (key, value) for key, value in self._clients.items()
                if value.get("uuid") == player_uuid
            ]
            player_tokens.sort(key=lambda pair: float(pair[1].get("created_at", 0)))
            while len(player_tokens) >= self.max_tokens_per_player:
                oldest, _ = player_tokens.pop(0)
                self._clients.pop(oldest, None)
            self._clients[digest] = {
                "uuid": player_uuid,
                "client_id": str(client_id or "")[:64],
                "created_at": current,
                "last_seen": current,
            }
            self._save()
            return player_uuid, token

    def resolve_token(self, token: str, now: float | None = None) -> tuple[str, str] | None:
        if not isinstance(token, str) or not 32 <= len(token) <= 160:
            return None
        current = time.time() if now is None else float(now)
        digest = _token_hash(token)
        with self._lock:
            record = self._clients.get(digest)
            if record is None:
                return None
            record["last_seen"] = current
            return str(record["uuid"]), digest

    def revoke_player(self, player_uuid: str) -> int:
        key = str(player_uuid or "")
        with self._lock:
            removed = [digest for digest, item in self._clients.items() if item.get("uuid") == key]
            for digest in removed:
                self._clients.pop(digest, None)
            if removed:
                self._save()
            return len(removed)

    def paired_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @staticmethod
    def make_proof(token: str, session_id: str, nonce: str) -> str:
        message = f"{PROTOCOL}|{session_id}|{nonce}".encode("utf-8")
        return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()

    @staticmethod
    def verify_payload(token: str, payload: dict[str, Any], signature: str) -> bool:
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        expected = hmac.new(token.encode("utf-8"), _canonical(unsigned), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature or ""))


def validate_telemetry(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    integrity_source = data.get("integrity", {})
    runtime_source = data.get("runtime", {})
    if not isinstance(integrity_source, dict) or not isinstance(runtime_source, dict):
        return None
    integrity_keys = {
        "app_signature_valid", "package_name_valid", "debugger_detected",
        "root_detected", "emulator_detected", "tamper_detected",
    }
    integrity: dict[str, bool | None] = {}
    for key in integrity_keys:
        value = integrity_source.get(key)
        if value is not None and not isinstance(value, bool):
            return None
        integrity[key] = value
    runtime: dict[str, int] = {}
    limits = {
        "app_uptime_seconds": (0, 604800),
        "fps": (0, 240),
        "memory_mb": (0, 32768),
        "heartbeat_ms": (0, 60000),
    }
    for key, (minimum, maximum) in limits.items():
        value = runtime_source.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = int(value)
        if number < minimum or number > maximum:
            return None
        runtime[key] = number
    platform = str(data.get("platform", "unknown"))[:24].lower()
    if platform not in {"android", "windows", "ios", "unknown"}:
        return None
    return {
        "client_version": str(data.get("client_version", ""))[:32],
        "build_id": str(data.get("build_id", ""))[:64],
        "platform": platform,
        "integrity": integrity,
        "runtime": runtime,
    }
