package com.markss.retailocr.data

import android.content.Context
import android.content.SharedPreferences
import com.markss.retailocr.BuildConfig
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * Runtime configuration: backend address and printer MAC.
 *
 * These were originally compile-time only, injected from `local.properties`
 * into BuildConfig. That is fine for a developer but useless in a store, where
 * the server address changes per site and nobody is going to rebuild an APK to
 * enter it. BuildConfig now supplies the default and preferences override it.
 *
 * Still no hard-coded endpoint anywhere (CLAUDE.md section 19) — the default
 * itself comes from build configuration, not source.
 */
object AppPreferences {

    private const val FILE = "retail_ocr_settings"
    private const val KEY_SERVER_URL = "server_url"
    private const val KEY_PRINTER_MAC = "printer_mac"
    const val ENGINE_BOTH = "both"
    const val ENGINE_VLM_ONLY = "vlm"

    private const val KEY_AI_ONLY = "ai_only_mode"
    private const val KEY_SAMPLE_MODE = "sample_mode"
    private const val KEY_BLUETOOTH_ASKED = "bluetooth_asked"

    private val MAC_PATTERN = Regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        prefs = context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)
    }

    /** Backend base URL, always normalized with a scheme and a trailing slash. */
    var serverUrl: String
        get() = prefs.getString(KEY_SERVER_URL, null)?.takeIf { it.isNotBlank() }
            ?: BuildConfig.BACKEND_BASE_URL
        set(value) = prefs.edit().putString(KEY_SERVER_URL, normalizeUrl(value)).apply()

    /**
     * Sample collection mode.
     *
     * When on, a capture is written to device storage instead of being
     * uploaded. Accuracy is currently measured against full-frame photographs,
     * which are a harder input than the app actually produces — it crops to the
     * ROI window first. This mode collects the real thing, so the measurement
     * matches what operators will see.
     */
    var sampleMode: Boolean
        get() = prefs.getBoolean(KEY_SAMPLE_MODE, false)
        set(value) = prefs.edit().putBoolean(KEY_SAMPLE_MODE, value).apply()

    /**
     * Read with the vision-language model alone, skipping PP-OCRv5.
     *
     * A switch rather than a rebuild because the two pipelines have to be
     * compared on the SAME packs to mean anything, and a pack is only in front
     * of the operator once. Both run identical extraction, normalization,
     * validation and confidence code on the server, so a difference in results
     * is a difference between the engines rather than between two sets of
     * rules.
     *
     * Off by default: PP-OCRv5 answers in about 6s where the model takes 95s,
     * and dropping it also drops the agreement signal that lets a field reach
     * HIGH at all.
     */
    var aiOnlyMode: Boolean
        get() = prefs.getBoolean(KEY_AI_ONLY, false)
        set(value) = prefs.edit().putBoolean(KEY_AI_ONLY, value).apply()

    /** Value for the scan request's engineMode field. */
    val engineMode: String
        get() = if (aiOnlyMode) ENGINE_VLM_ONLY else ENGINE_BOTH

    /**
     * Whether the Bluetooth permission prompt has been shown at least once.
     *
     * Needed to tell "never asked" from "denied permanently". Android reports
     * both as no-rationale-needed, so without this the first tap would send the
     * operator to Settings instead of showing them the prompt.
     */
    var bluetoothAsked: Boolean
        get() = prefs.getBoolean(KEY_BLUETOOTH_ASKED, false)
        set(value) = prefs.edit().putBoolean(KEY_BLUETOOTH_ASKED, value).apply()

    /** Zebra ZQ320 Bluetooth MAC. Consumed by printing in Phase 5. */
    var printerMac: String
        get() = prefs.getString(KEY_PRINTER_MAC, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PRINTER_MAC, value.uppercase().trim()).apply()

    /**
     * Accepts what an operator will actually type.
     *
     * "192.168.1.50:8000", "192.168.1.50", and a full URL all normalize to the
     * same thing. Retrofit rejects a base URL without a trailing slash, and a
     * missing scheme would otherwise fail much later with an opaque error.
     */
    fun normalizeUrl(input: String): String {
        var value = input.trim()
        if (value.isEmpty()) return value
        if (!value.startsWith("http://", true) && !value.startsWith("https://", true)) {
            value = "http://$value"
        }
        if (!value.endsWith("/")) value = "$value/"
        return value
    }

    fun isValidUrl(input: String): Boolean =
        normalizeUrl(input).toHttpUrlOrNull() != null

    /** Empty is valid: the printer is optional until Phase 5 is wired up. */
    fun isValidMac(input: String): Boolean =
        input.isBlank() || MAC_PATTERN.matches(input.trim())
}
