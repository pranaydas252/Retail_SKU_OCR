package com.markss.dmartocr.device

import android.annotation.SuppressLint
import android.content.Context
import android.provider.Settings

/**
 * Stable per-device identifier for the scan audit trail.
 *
 * Not [android.os.Build.SERIAL]: that is deprecated, and on API 26 and above it
 * returns the literal string "unknown" unless the app holds READ_PHONE_STATE —
 * which would put a runtime permission prompt in front of the operator for no
 * benefit. ANDROID_ID is stable for the lifetime of the install, needs no
 * permission, and is enough to tell one TC22's scans from another's in the
 * `SkuScan.DeviceId` column.
 */
object DeviceId {

    @SuppressLint("HardwareIds")
    fun of(context: Context): String =
        Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?: "unknown"
}
