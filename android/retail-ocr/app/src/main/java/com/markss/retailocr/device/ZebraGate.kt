package com.markss.retailocr.device

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import com.markss.retailocr.BuildConfig

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
 * 2. The manifest MUST declare a <queries> element for every package named in
 *    [ZEBRA_SYSTEM_PACKAGES]. Without it, [PackageManager] reports "not found"
 *    on every device including genuine Zebra hardware, and this gate rejects
 *    everything while looking like a hardware fault (PLAN.md R11).
 *
 * ## Why not EMDK
 *
 * The original implementation required `com.symbol.emdk.emdkservice`, on the
 * assumption that the EMDK runtime ships on every Zebra device. Checked
 * against a real TC22 running Android 14, it does not — the EMDK runtime is a
 * separate install, and that device carries none of it while carrying 115
 * other Zebra and Symbol system packages. Requiring EMDK would have blocked
 * the exact hardware this app is built for.
 *
 * The corroborating signal is now Zebra's MX management framework
 * (`com.symbol.mxmf`), which is present as a system package on Zebra Android
 * devices and is what actually distinguishes the platform. EMDK is still
 * accepted if present, but is no longer required.
 */
object ZebraGate {

    private const val TAG = "ZebraGate"

    /**
     * Packages that indicate the Zebra Android platform, in preference order.
     *
     * Every entry here must also appear in the manifest's <queries> element or
     * it will never be found.
     */
    private val ZEBRA_SYSTEM_PACKAGES = listOf(
        // MX management framework. Present on Zebra Android devices generally
        // and the most reliable single indicator.
        "com.symbol.mxmf",
        // Zebra's scanning middleware. Not used by this app for capture, but a
        // strong platform signal.
        "com.symbol.datawedge",
        // EMDK runtime. Optional — absent on the TC22 this was verified against.
        "com.symbol.emdk.emdkservice",
    )

    /**
     * Older Zebra units report as Motorola Solutions. Both must be accepted or
     * genuine hardware gets rejected.
     */
    private val ZEBRA_MANUFACTURERS = listOf("zebra technologies", "motorola solutions")

    sealed interface Result {
        data class Allowed(val evidence: String) : Result

        /** Debug build with the gate bypassed. Never reachable in release. */
        data class Bypassed(val device: String) : Result

        data class Blocked(val device: String, val reason: String) : Result
    }

    fun check(context: Context): Result {
        val device = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

        val manufacturerOk = ZEBRA_MANUFACTURERS.any {
            Build.MANUFACTURER.lowercase().contains(it)
        }
        val platformPackage = ZEBRA_SYSTEM_PACKAGES.firstOrNull {
            isSystemPackageInstalled(context, it)
        }

        if (manufacturerOk && platformPackage != null) {
            Log.i(TAG, "Zebra device verified: $device via $platformPackage")
            return Result.Allowed(platformPackage)
        }

        val reason = when {
            !manufacturerOk && platformPackage == null ->
                "not a Zebra device and no Zebra platform packages"

            !manufacturerOk -> "manufacturer is ${Build.MANUFACTURER}"
            else -> "no Zebra platform package found"
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
     * True when the named package is installed as a system package.
     *
     * The system-flag test matters: a sideloaded app could otherwise take a
     * Zebra package name and satisfy the gate. It costs nothing and closes the
     * easiest way to fake the check short of rooting.
     */
    private fun isSystemPackageInstalled(context: Context, packageName: String): Boolean = try {
        val info = context.packageManager.getApplicationInfo(packageName, 0)
        val isSystem = (info.flags and
            (ApplicationInfo.FLAG_SYSTEM or ApplicationInfo.FLAG_UPDATED_SYSTEM_APP)) != 0
        if (!isSystem) {
            Log.w(TAG, "$packageName present but not a system package; ignoring")
        }
        isSystem
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }
}
