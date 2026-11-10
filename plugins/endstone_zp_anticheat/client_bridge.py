from __future__ import annotations

import json
import queue
import secrets
import socketserver
import ssl
import threading
import time
from pathlib import Path
from typing import Any

from .client_security import NativeClientRegistry, PROTOCOL, validate_telemetry

MAX_LINE_BYTES = 8192


class _ThreadedTLSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], handler: type, context: ssl.SSLContext, owner: Any):
        self.tls_context = context
        self.owner = owner
        super().__init__(address, handler)

    def get_request(self):
        raw_socket, address = super().get_request()
        try:
            raw_socket.settimeout(10.0)
            wrapped = self.tls_context.wrap_socket(raw_socket, server_side=True)
            return wrapped, address
        except Exception:
            raw_socket.close()
            raise


class _ClientHandler(socketserver.StreamRequestHandler):
    def _send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self.wfile.write(encoded)
        self.wfile.flush()

    def _read(self) -> dict[str, Any] | None:
        raw = self.rfile.readline(MAX_LINE_BYTES + 1)
        if not raw or len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def handle(self) -> None:
        owner = self.server.owner
        active_uuid = ""
        self.request.settimeout(15.0)
        try:
            first = self._read()
            if not first or first.get("protocol") != PROTOCOL:
                return
            if first.get("type") == "pair":
                remote_ip = str(self.client_address[0]) if self.client_address else "unknown"
                if not owner.allow_pair_attempt(remote_ip):
                    self._send({"type": "error", "code": "pair_rate_limited"})
                    return
                paired = owner.registry.consume_pair_code(
                    str(first.get("code", "")), str(first.get("client_id", ""))
                )
                if paired is None:
                    self._send({"type": "error", "code": "invalid_or_expired_pair_code"})
                    return
                player_uuid, token = paired
                self._send({
                    "type": "paired", "protocol": PROTOCOL,
                    "player_uuid": player_uuid, "token": token,
                    "warning": "Store this token securely. It is shown only once.",
                })
                try:
                    owner.events.put_nowait({"kind": "paired", "player_uuid": player_uuid, "at": time.time()})
                except queue.Full:
                    pass
                return
            if first.get("type") != "hello":
                return
            token = str(first.get("token", ""))
            resolved = owner.registry.resolve_token(token)
            if resolved is None:
                self._send({"type": "error", "code": "invalid_token"})
                return
            player_uuid, token_digest = resolved
            session_id = secrets.token_urlsafe(18)
            nonce = secrets.token_urlsafe(32)
            self._send({
                "type": "challenge", "protocol": PROTOCOL,
                "session_id": session_id, "nonce": nonce,
                "expires_in_seconds": 30,
            })
            self.request.settimeout(30.0)
            attestation = self._read()
            expected = owner.registry.make_proof(token, session_id, nonce)
            import hmac
            if (
                not attestation or attestation.get("type") != "attest"
                or attestation.get("session_id") != session_id
                or not hmac.compare_digest(str(attestation.get("proof", "")), expected)
            ):
                self._send({"type": "error", "code": "challenge_failed"})
                return
            self._send({"type": "ready", "protocol": PROTOCOL, "session_id": session_id, "heartbeat_seconds": 10})
            active_uuid = player_uuid
            owner._register_connection(player_uuid, self.request)
            try:
                owner.events.put_nowait({"kind": "connected", "player_uuid": player_uuid, "at": time.time()})
            except queue.Full:
                owner._unregister_connection(player_uuid, self.request)
                active_uuid = ""
                return
            last_sequence = 0
            last_received_at = 0.0
            self.request.settimeout(25.0)
            while not owner.stopping.is_set():
                message = self._read()
                if message is None:
                    break
                if message.get("type") != "telemetry" or message.get("session_id") != session_id:
                    continue
                sequence = message.get("seq")
                timestamp = message.get("timestamp")
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= last_sequence:
                    continue
                if time.monotonic() - last_received_at < 1.0:
                    continue
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or abs(time.time() - float(timestamp)) > 120:
                    continue
                data = validate_telemetry(message.get("data"))
                if data is None or not owner.registry.verify_payload(token, message, str(message.get("signature", ""))):
                    continue
                received_at = time.monotonic()
                try:
                    owner.events.put_nowait({
                        "kind": "telemetry",
                        "player_uuid": player_uuid,
                        "token_hash": token_digest,
                        "sequence": sequence,
                        "data": data,
                        "at": time.time(),
                    })
                except queue.Full:
                    continue
                last_sequence = sequence
                last_received_at = received_at
                try:
                    self._send({"type": "ack", "seq": sequence})
                except Exception:
                    break
        except (OSError, ssl.SSLError, TimeoutError, ValueError):
            return
        finally:
            if active_uuid:
                owner._unregister_connection(active_uuid, self.request)
                try:
                    owner.events.put_nowait({"kind": "disconnected", "player_uuid": active_uuid, "at": time.time()})
                except queue.Full:
                    pass
            try:
                self.request.close()
            except Exception:
                pass


class NativeClientBridge:
    """Optional TLS-only native client transport. Network threads never call Endstone APIs."""

    def __init__(
        self,
        registry: NativeClientRegistry,
        events: queue.Queue,
        host: str,
        port: int,
        certificate: str,
        private_key: str,
        logger: Any,
    ) -> None:
        self.registry = registry
        self.events = events
        self.host = str(host)
        self.port = int(port)
        self.certificate = Path(certificate) if certificate else None
        self.private_key = Path(private_key) if private_key else None
        self.logger = logger
        self.server = None
        self.thread = None
        self.stopping = threading.Event()
        self.error = ""
        self._connection_lock = threading.RLock()
        self._active_sockets: dict[str, set[Any]] = {}
        self._pair_attempts: dict[str, tuple[float, int]] = {}

    def allow_pair_attempt(self, remote_ip: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._connection_lock:
            started, attempts = self._pair_attempts.get(remote_ip, (current, 0))
            if current - started >= 300.0:
                started, attempts = current, 0
            if attempts >= 8:
                self._pair_attempts[remote_ip] = (started, attempts)
                return False
            self._pair_attempts[remote_ip] = (started, attempts + 1)
            if len(self._pair_attempts) > 4096:
                self._pair_attempts = {
                    ip: value for ip, value in self._pair_attempts.items()
                    if current - value[0] < 300.0
                }
            return True

    def start(self) -> bool:
        if self.server is not None:
            return True
        if self.certificate is None or self.private_key is None:
            self.error = "TLS certificate/private key not configured"
            self.logger.warning("ZP-AC 原生客户端桥接已启用但未配置 TLS 证书/私钥；监听保持关闭。")
            return False
        if not self.certificate.is_file() or not self.private_key.is_file():
            self.error = "TLS certificate/private key file missing"
            self.logger.warning("ZP-AC 原生客户端桥接 TLS 文件不存在；监听保持关闭。")
            return False
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(self.certificate), str(self.private_key))
            self.server = _ThreadedTLSServer(
                (self.host, self.port), _ClientHandler, context, self
            )
            self.port = int(self.server.server_address[1])
            self.stopping.clear()
            self.thread = threading.Thread(
                target=self.server.serve_forever, name="ZPAC-NativeClientBridge", daemon=True
            )
            self.thread.start()
            self.error = ""
            self.logger.info(f"ZP-AC 原生客户端 TLS 桥接已监听 {self.host}:{self.port}")
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.logger.error(f"ZP-AC 原生客户端桥接启动失败：{self.error}")
            self.stop()
            return False

    def _register_connection(self, player_uuid: str, client_socket: Any) -> None:
        with self._connection_lock:
            self._active_sockets.setdefault(player_uuid, set()).add(client_socket)

    def _unregister_connection(self, player_uuid: str, client_socket: Any) -> None:
        with self._connection_lock:
            sockets = self._active_sockets.get(player_uuid)
            if sockets is not None:
                sockets.discard(client_socket)
                if not sockets:
                    self._active_sockets.pop(player_uuid, None)

    def disconnect_player(self, player_uuid: str) -> int:
        with self._connection_lock:
            sockets = list(self._active_sockets.pop(str(player_uuid), set()))
        for client_socket in sockets:
            try:
                client_socket.shutdown(2)
            except Exception:
                pass
            try:
                client_socket.close()
            except Exception:
                pass
        return len(sockets)

    def stop(self) -> None:
        self.stopping.set()
        with self._connection_lock:
            all_sockets = [sock for group in self._active_sockets.values() for sock in group]
            self._active_sockets.clear()
        for sock in all_sockets:
            try:
                sock.shutdown(2)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        server = self.server
        self.server = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        thread = self.thread
        self.thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.server is not None,
            "host": self.host,
            "port": self.port,
            "error": self.error,
        }
