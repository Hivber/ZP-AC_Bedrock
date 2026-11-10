package com.hivber.zpac

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Debug
import android.os.SystemClock
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * Best-effort Android app/runtime checks for a ZP-AC-integrated client.
 * These checks are telemetry only: root/debugger/emulator signals are not proof of cheating.
 */
object ZpAcAndroidChecks {
    data class Snapshot(val integrity: JSONObject, val runtime: JSONObject)

    fun collect(
        context: Context,
        expectedPackageName: String,
        expectedSignerSha256: String,
        appStartedAtElapsedRealtime: Long,
    ): Snapshot {
        val packageNameValid = context.packageName == expectedPackageName
        val signerValid = try {
            val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                context.packageManager.getPackageInfo(
                    context.packageName,
                    PackageManager.GET_SIGNING_CERTIFICATES,
                )
            } else {
                @Suppress("DEPRECATION")
                context.packageManager.getPackageInfo(
                    context.packageName,
                    PackageManager.GET_SIGNATURES,
                )
            }
            val signatures = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                info.signingInfo?.apkContentsSigners
            } else {
                @Suppress("DEPRECATION")
                info.signatures
            }
            val expected = expectedSignerSha256.replace(":", "").lowercase()
            signatures?.any { signature ->
                val digest = MessageDigest.getInstance("SHA-256")
                    .digest(signature.toByteArray())
                    .joinToString("") { "%02x".format(it) }
                digest.equals(expected, ignoreCase = true)
            } == true
        } catch (_: Exception) {
            false
        }

        val rootDetected = listOf(
            "/system/bin/su",
            "/system/xbin/su",
            "/sbin/su",
            "/system/app/Superuser.apk",
            "/data/adb/magisk",
        ).any { File(it).exists() } || Build.TAGS?.contains("test-keys") == true

        val emulatorDetected =
            Build.FINGERPRINT.startsWith("generic") ||
            Build.FINGERPRINT.startsWith("unknown") ||
            Build.MODEL.contains("Emulator", ignoreCase = true) ||
            Build.MODEL.contains("Android SDK built for", ignoreCase = true) ||
            Build.HARDWARE.contains("goldfish", ignoreCase = true) ||
            Build.HARDWARE.contains("ranchu", ignoreCase = true) ||
            Build.PRODUCT.contains("sdk", ignoreCase = true)

        val debuggerDetected = Debug.isDebuggerConnected() || Debug.waitingForDebugger()
        val integrity = JSONObject()
            .put("app_signature_valid", signerValid)
            .put("package_name_valid", packageNameValid)
            .put("debugger_detected", debuggerDetected)
            .put("root_detected", rootDetected)
            .put("emulator_detected", emulatorDetected)
            .put("tamper_detected", !signerValid || !packageNameValid)

        val usedMemoryMb = (
            Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()
        ).coerceAtLeast(0L) / (1024L * 1024L)
        val uptimeSeconds = (
            (SystemClock.elapsedRealtime() - appStartedAtElapsedRealtime).coerceAtLeast(0L) / 1000L
        )
        val runtime = JSONObject()
            .put("app_uptime_seconds", uptimeSeconds.coerceAtMost(604800L))
            .put("memory_mb", usedMemoryMb.coerceAtMost(32768L))
            .put("heartbeat_ms", 0)
        return Snapshot(integrity, runtime)
    }
}
