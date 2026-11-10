package com.zpac.nativeclient

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import org.json.JSONArray
import org.json.JSONObject

/**
 * Reference transport SDK for a dedicated Android Bedrock client.
 *
 * This class does not inspect Minecraft by itself. The host client must call
 * reportTelemetry() from its own trusted input/render/network instrumentation.
 * The API endpoint must be HTTPS with a certificate trusted by Android.
 */
class NativeAttestationClient(
    private val baseUrl: String,
    private val playerUuid: String,
    private val clientVersion: String,
    private val buildId: String,
    private val keyAlias: String = "zp_ac_native_" +
        MessageDigest.getInstance("SHA-256").digest(playerUuid.toByteArray())
            .take(12).joinToString("") { "%02x".format(it) }
) {
    companion object {
        const val PROTOCOL = "zp-ac-native-v1"
        private const val KEYSTORE = "AndroidKeyStore"
    }

    private val normalizedBaseUrl = baseUrl.trimEnd('/')

    init {
        require(normalizedBaseUrl.startsWith("https://")) {
            "ZP-AC native attestation requires HTTPS"
        }
        require(playerUuid.isNotBlank())
    }

    private fun keyPair(): KeyPair {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        val privateKey = store.getKey(keyAlias, null) as? java.security.PrivateKey
        val certificate = store.getCertificate(keyAlias)
        if (privateKey != null && certificate != null) {
            return KeyPair(certificate.publicKey, privateKey)
        }
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KEYSTORE)
        val spec = KeyGenParameterSpec.Builder(
            keyAlias,
            KeyProperties.PURPOSE_SIGN
        )
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setUserAuthenticationRequired(false)
            .build()
        generator.initialize(spec)
        return generator.generateKeyPair()
    }

    private fun keyId(pair: KeyPair): String =
        MessageDigest.getInstance("SHA-256")
            .digest(pair.public.encoded)
            .take(16)
            .joinToString("") { "%02x".format(it) }

    private fun publicKeyPem(pair: KeyPair): String {
        val encoded = Base64.getMimeEncoder(64, byteArrayOf(10)).encodeToString(pair.public.encoded)
        return "-----BEGIN PUBLIC KEY-----\n$encoded\n-----END PUBLIC KEY-----"
    }

    private fun sign(pair: KeyPair, message: ByteArray): String {
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(pair.private)
        signer.update(message)
        return Base64.getEncoder().encodeToString(signer.sign())
    }

    private fun quote(value: String): String = JSONObject.quote(value)

    private fun canonicalValue(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is String -> quote(value)
        is Boolean, is Number -> value.toString()
        is JSONObject -> canonicalJson(value)
        is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") {
            canonicalValue(value.get(it))
        }
        else -> quote(value.toString())
    }

    private fun canonicalJson(value: JSONObject): String {
        val keys = value.keys().asSequence().toList().sorted()
        return keys.joinToString(",", "{", "}") { key ->
            quote(key) + ":" + canonicalValue(value.get(key))
        }
    }

    private fun post(path: String, payload: JSONObject): JSONObject {
        val connection = URL(normalizedBaseUrl + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.connectTimeout = 5000
            connection.readTimeout = 5000
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.setRequestProperty("Accept", "application/json")
            val bytes = payload.toString().toByteArray(Charsets.UTF_8)
            require(bytes.size <= 16 * 1024) { "attestation request too large" }
            connection.outputStream.use { it.write(bytes) }
            val stream = if (connection.responseCode in 200..299) {
                connection.inputStream
            } else {
                connection.errorStream
            }
            val body = BufferedReader(InputStreamReader(stream, Charsets.UTF_8)).use { it.readText() }
            val result = JSONObject(body)
            check(connection.responseCode in 200..299 && result.optBoolean("ok")) {
                "ZP-AC attestation rejected: " + result.optString("error", "http_${connection.responseCode}")
            }
            return result
        } finally {
            connection.disconnect()
        }
    }

    /** Pair once using the one-time code issued by /zpac client pair <player>. */
    fun enroll(pairingCode: String): JSONObject {
        val pair = keyPair()
        val id = keyId(pair)
        val proofMessage = "zp-ac-enroll-v1|$playerUuid|$id|$pairingCode".toByteArray(Charsets.UTF_8)
        return post(
            "/v1/enroll",
            JSONObject()
                .put("player_uuid", playerUuid)
                .put("key_id", id)
                .put("public_key_pem", publicKeyPem(pair))
                .put("pairing_code", pairingCode)
                .put("proof", sign(pair, proofMessage))
        )
    }

    /**
     * Submit a signed snapshot. integrityClaims are client-reported hints, not
     * proof of process integrity; server-side checks remain authoritative.
     */
    fun reportTelemetry(
        capabilities: List<String>,
        integrityClaims: Map<String, Boolean>,
        telemetry: JSONObject = JSONObject()
    ): JSONObject {
        val pair = keyPair()
        val id = keyId(pair)
        val challenge = post(
            "/v1/challenge",
            JSONObject().put("player_uuid", playerUuid).put("key_id", id)
        )
        val payload = JSONObject()
            .put("protocol", PROTOCOL)
            .put("player_uuid", playerUuid)
            .put("key_id", id)
            .put("challenge_id", challenge.getString("challenge_id"))
            .put("nonce", challenge.getString("nonce"))
            .put("sequence", challenge.getLong("sequence"))
            .put("client_version", clientVersion)
            .put("build_id", buildId)
            .put("capabilities", JSONArray(capabilities.take(32)))
            .put("integrity", JSONObject().also { obj ->
                integrityClaims.entries.take(24).forEach { (key, value) -> obj.put(key, value) }
            })
            .put("telemetry", telemetry)
        payload.put("signature", sign(pair, canonicalJson(payload).toByteArray(Charsets.UTF_8)))
        return post("/v1/attest", payload)
    }

    /** Indicates whether Android reports this Keystore key as hardware-backed. */
    fun isHardwareBackedKey(): Boolean = try {
        val pair = keyPair()
        val factory = KeyFactory.getInstance(pair.public.algorithm, KEYSTORE)
        val info = factory.getKeySpec(pair.private, KeyInfo::class.java)
        info.isInsideSecureHardware
    } catch (_: Exception) {
        false
    }
}
