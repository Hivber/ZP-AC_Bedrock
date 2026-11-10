package com.hivber.zpac

import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.Closeable
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetSocketAddress
import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory

/**
 * Small Android SDK transport for ZP-AC's optional native-client bridge.
 *
 * The host app must:
 *  - collect only checks it can legitimately perform;
 *  - store the returned pairing token in Android Keystore-backed storage;
 *  - run network calls on a background executor;
 *  - pin the server certificate SHA-256 fingerprint out-of-band.
 *
 * This transport does not itself inspect Minecraft memory or prove that the
 * game process is unmodified. Self-reported integrity flags are advisory only.
 */
class ZpAcClientBridge(
    private val host: String,
    private val port: Int,
    private val certificateSha256: String,
    private val socketFactory: SSLSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory,
) : Closeable {
    private var socket: SSLSocket? = null
    private var reader: BufferedReader? = null
    private var writer: BufferedWriter? = null
    private var token: String? = null
    private var sessionId: String? = null
    private var sequence: Long = 0

    fun pair(oneTimeCode: String, clientId: String): String {
        openSocket()
        send(JSONObject().put("protocol", PROTOCOL).put("type", "pair")
            .put("code", oneTimeCode).put("client_id", clientId))
        val response = receive()
        close()
        check(response.optString("type") == "paired") { "Pairing failed: ${response.optString("code")}" }
        val issued = response.getString("token")
        token = issued
        return issued
    }

    fun connect(savedToken: String, clientId: String) {
        token = savedToken
        openSocket()
        send(JSONObject().put("protocol", PROTOCOL).put("type", "hello")
            .put("client_id", clientId).put("token", savedToken))
        val challenge = receive()
        check(challenge.optString("type") == "challenge") { "Challenge rejected" }
        val currentSession = challenge.getString("session_id")
        val nonce = challenge.getString("nonce")
        val proof = hmac(savedToken, "${PROTOCOL}|${currentSession}|${nonce}")
        send(JSONObject().put("type", "attest").put("session_id", currentSession).put("proof", proof))
        val ready = receive()
        check(ready.optString("type") == "ready") { "Native client authentication failed" }
        sessionId = currentSession
        sequence = 0
    }

    fun sendTelemetry(
        clientVersion: String,
        buildId: String,
        integrity: JSONObject,
        runtime: JSONObject,
    ) {
        val activeToken = requireNotNull(token) { "Client is not paired" }
        val activeSession = requireNotNull(sessionId) { "Client is not connected" }
        sequence += 1
        val unsigned = JSONObject()
            .put("type", "telemetry")
            .put("session_id", activeSession)
            .put("seq", sequence)
            .put("timestamp", System.currentTimeMillis() / 1000.0)
            .put("data", JSONObject()
                .put("client_version", clientVersion)
                .put("build_id", buildId)
                .put("platform", "android")
                .put("integrity", integrity)
                .put("runtime", runtime))
        val canonical = canonical(unsigned)
        unsigned.put("signature", hmac(activeToken, canonical))
        send(unsigned)
        val ack = receive()
        check(ack.optString("type") == "ack" && ack.optLong("seq") == sequence) { "Telemetry acknowledgement mismatch" }
    }

    private fun openSocket() {
        val created = socketFactory.createSocket() as SSLSocket
        created.connect(InetSocketAddress(host, port), 5000)
        created.soTimeout = 15000
        created.startHandshake()
        val cert = created.session.peerCertificates.first()
        val actual = MessageDigest.getInstance("SHA-256").digest(cert.encoded)
            .joinToString("") { "%02x".format(it) }
        check(actual.equals(certificateSha256.replace(":", "").lowercase(), ignoreCase = true)) {
            created.close()
            error("Server certificate fingerprint mismatch")
        }
        socket = created
        reader = BufferedReader(InputStreamReader(created.inputStream, Charsets.UTF_8))
        writer = BufferedWriter(OutputStreamWriter(created.outputStream, Charsets.UTF_8))
    }

    private fun send(value: JSONObject) {
        val output = requireNotNull(writer)
        output.write(value.toString())
        output.write("\n")
        output.flush()
    }

    private fun receive(): JSONObject {
        val line = requireNotNull(reader).readLine() ?: error("Server disconnected")
        require(line.length <= 8192) { "Server message too large" }
        return JSONObject(line)
    }

    private fun hmac(key: String, message: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(message.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }

    private fun canonical(value: JSONObject): String {
        val keys = value.keys().asSequence().toList().sorted()
        return JSONObject().also { sorted -> keys.forEach { sorted.put(it, value.get(it)) } }.toString()
    }

    override fun close() {
        try { socket?.close() } catch (_: Exception) {}
        socket = null
        reader = null
        writer = null
        sessionId = null
    }

    companion object {
        const val PROTOCOL = "zp-ac-native-v1"
    }
}
