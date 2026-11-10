# ZP-AC dual-mode client security

## Two modes

1. **Bedrock add-on mode (default):** the behavior pack and Endstone observe server-side player events. This is not client-side attestation. Bedrock Script API does not grant arbitrary access to Android processes, game memory, DLLs, or other apps.
2. **Optional native-client mode:** a separately integrated Android client SDK connects to the Endstone TLS bridge. It can report app/runtime checks that the host client actually implements. It does not make the official Bedrock client modifiable and does not prove that the game process is cheat-free.

Both modes feed evidence into the same Endstone review pipeline. Native reports are advisory and never directly kick, ban, or alter credit/trust scores.

## Server setup

Native mode is disabled by default. Configure these values in the Endstone plugin config:

- `native_client_bridge_enabled = true`
- `native_client_bridge_host = "0.0.0.0"`
- `native_client_bridge_port = 49376`
- `native_client_tls_cert = "/absolute/path/server.crt"`
- `native_client_tls_key = "/absolute/path/server.key"`

Use a valid TLS certificate and firewall the port to the required client network. Do not expose the bridge over plaintext TCP. The plugin refuses to start the listener without both TLS files.

For a private server, a self-signed certificate can be pinned by the Android SDK (the exact SHA-256 fingerprint is the trust anchor):

```sh
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
  -keyout server.key -out server.crt -subj "/CN=your-server-host"
chmod 600 server.key
openssl x509 -in server.crt -noout -fingerprint -sha256
```

Copy the certificate fingerprint (remove colons) into the Android client's `certificateSha256`. Keep the private key server-side only. When rotating the certificate, update the pinned fingerprint in the client build.

Then restart Endstone and run `/zpac clientstatus`. To pair a player, an administrator runs `/zpac clientpair <online-player>`; the server privately sends that player a single-use code valid for five minutes. Enter it in the integrated native client. The app receives a per-device bearer token once and must store it using Android Keystore-backed encryption. Use `/zpac clientrevoke <online-player>` to revoke all paired devices for that UUID.

## Wire protocol

Newline-delimited UTF-8 JSON over TLS 1.2+:

1. Pair: `pair` + one-time code; server returns a random per-device token once.
2. Connect: `hello` + token; server returns a fresh session ID and random nonce.
3. Prove possession: client sends HMAC-SHA256(token, protocol|session_id|nonce).
4. Telemetry: strictly increasing sequence, server-issued session ID, timestamp, bounded schema, and HMAC over canonical JSON.
5. Server rejects invalid tokens, expired/replayed challenges, bad signatures, out-of-window timestamps, duplicate sequence numbers, oversized frames and invalid field ranges.

The server stores only SHA-256 token hashes and player UUIDs. Pairing codes and active transport sessions are memory-only. Network threads never call Endstone APIs; validated telemetry is queued and processed on the server scheduler.

## Trust model and limitations

- TLS protects data in transit; the client pins the server certificate fingerprint.
- HMAC proves possession of a paired token and protects message integrity, not that the client is honest. A rooted or modified client can extract a token or forge its own self-reported flags.
- Android package-signature checks, debugger/root heuristics and Play Integrity must be implemented by the host APK and independently verified where applicable. The included Kotlin class is a transport SDK, not a completed game client or a Play Integrity implementation.
- Bedrock add-on mode remains fully compatible and does not require native pairing.
- No native-client report automatically causes punishment. Server-side packet/event evidence remains authoritative.

## Android SDK

The reference sources are:

- `android/ZpAcClientBridge.kt`: TLS pinning, one-time pairing, challenge-response and signed telemetry transport.
- `android/ZpAcAndroidChecks.kt`: best-effort APK package/signing-certificate, debugger, root and emulator checks plus basic app runtime measurements.
- `android/ZpAcTokenVault.kt`: AES-GCM token storage backed by Android Keystore.

Example integration (run all network calls on a background executor):

```kotlin
val token = bridge.pair(pairCode, Build.FINGERPRINT)
ZpAcTokenVault.save(context, token)

// On later launches:
val savedToken = ZpAcTokenVault.load(context) ?: return
bridge.connect(savedToken, Build.FINGERPRINT)

val appStartedAt = SystemClock.elapsedRealtime()
val snapshot = ZpAcAndroidChecks.collect(
    context,
    expectedPackageName = BuildConfig.APPLICATION_ID,
    expectedSignerSha256 = EXPECTED_RELEASE_SIGNER_SHA256,
    appStartedAtElapsedRealtime = appStartedAt,
)
bridge.sendTelemetry(
    clientVersion = BuildConfig.VERSION_NAME,
    buildId = BuildConfig.VERSION_CODE.toString(),
    integrity = snapshot.integrity,
    runtime = snapshot.runtime,
    gameTelemetry = JSONObject()
        .put("client_pos_x", gamePosition.x)
        .put("client_pos_y", gamePosition.y)
        .put("client_pos_z", gamePosition.z)
        .put("network_rtt_ms", measuredRttMs),
)
```

Call `sendTelemetry` periodically (the transport accepts at most one report per second; a 10-second interval is recommended), and close/reconnect when the app returns from background or the network changes. The optional gameTelemetry object may contain the latest client-side position and measured RTT. Endstone compares it to the authoritative server position as low-confidence review evidence only. The host app must obtain the expected signer fingerprint from its release APK signing certificate and pin the server certificate fingerprint out-of-band.

These Android checks are heuristics: root/debugger/emulator signals can be false positives or hidden, and a modified client can falsify all local results. This SDK does not inspect Minecraft memory, native game code, other processes, or gameplay input. Actual gameplay anti-cheat remains server-authoritative.
