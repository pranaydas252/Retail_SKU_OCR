package com.markss.dmartocr.device

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import com.markss.dmartocr.BuildConfig

/**
 * Zebra-only device restriction (CLAUDE.md section 4).
 *
 * The app must refuse to operate on non-Zebra hardware. Two things about that
 * are worth stating plainly:
 *
 * 1. This is a runtime check, not a build-time one. The APK still installs
 *    anywhere, and [Build.MANUFACTURER] is spoofable on a rooted device. That
 *    is accepted: the app is useless off a TC22 because the scan flow refuses
 *    to start, and distribution is a controlled store estate. There is
 *    deliberately no server-side allowlist (PLAN.md R12).
 *
 * 2. The manifest MUST declare
 *        <queries><package android:name="com.symbol.emdk.emdkservice" /></queries>
 *    Without it, [PackageManager] returns "not found" on every device
 *    including genuine Zebra hardware, and this gate rejects everything while
 *    looking like a hardware fault (PLAN.md R11).
 */
object ZebraGate {

    private const val TAG = "ZebraGate"

    /** Zebra's EMDK runtime. Present only on Zebra devices. */
    private const val EMDK_SERVICE_PACKAGE = "com.symbol.emdk.emdkservice"

    /**
     * Older Zebra units report as Motorola Solutions. Both must be accepted or
     * genuine hardware gets rejected.
     */
    private val ZEBRA_MANUFACTURERS = listOf("zebra technologies", "motorola solutions")

    sealed interface Result {
        data object Allowed : Result

        /** Debug build with the gate bypassed. Never reachable in release. */
        data class Bypassed(val device: String) : Result

        data class Blocked(val device: String, val reason: String) : Result
    }

    fun check(context: Context): Result {
        val device = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

        val manufacturerOk = ZEBRA_MANUFACTURERS.any {
            Build.MANUFACTURER.lowercase().contains(it)
        }
        val emdkPresent = isEmdkRuntimePresent(context)

        if (manufacturerOk && emdkPresent) return Result.Allowed

        val reason = when {
            !manufacturerOk && !emdkPresent -> "not a Zebra device and no EMDK runtime"
            !manufacturerOk -> "manufacturer is ${Build.MANUFACTURER}"
            else -> "EMDK runtime not installed"
        }

        if (!BuildConfig.ENFORCE_ZEBRA_ONLY) {
            // Loud on purpose. A silent bypass is how a debug build reaches a
            // store floor without anyone noticing.
            Log.w(
                TAG,
                "ZEBRA-ONLY GATE BYPASSED — debug build running on '$device' ($reason). " +
                    "This must never happen in a release build."
            )
            return Result.Bypassed(device)
        }

        Log.w(TAG, "Blocked non-Zebra device '$device': $reason")
        return Result.Blocked(device, reason)
    }

    /**
     * True when Zebra's EMDK runtime is installed.
     *
     * This is the substantive half of the check: the EMDK service ships as part
     * of the Zebra software stack, so its presence is a far better signal than
     * a build string. Full `EMDKManager.getEMDKManager()` initialization is the
     * third step described in section 4 and plugs in here once the EMDK
     * artifact is available (PLAN.md Q4/R6); the SDK is not required for this
     * package-level check, which keeps the project buildable without Zebra
     * repository access.
     */
    private fun isEmdkRuntimePresent(context: Context): Boolean = try {
        context.packageManager.getPackageInfo(EMDK_SERVICE_PACKAGE, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }
}
