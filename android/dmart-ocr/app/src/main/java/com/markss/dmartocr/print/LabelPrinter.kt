package com.markss.dmartocr.print

/**
 * Printing abstraction (CLAUDE.md section 18).
 *
 * Only the implementation may know about transports. Connection handling,
 * retries and ZPL construction stay behind this interface so OCR logic and
 * printer logic never couple.
 */
interface LabelPrinter {

    suspend fun print(scan: ConfirmedScan): PrintResult

    /**
     * Problems detectable *without* the Bluetooth runtime permission, or null
     * when printing is worth attempting.
     *
     * Exists so the caller never asks for a permission it is about to waste. If
     * no printer is configured, or the radio is off, the answer is the same
     * whether or not permission is granted — prompting first would make the
     * operator grant something and only then be told it was pointless.
     */
    fun preflight(): PrintResult.Failure?
}

/**
 * What gets printed. Deliberately not the API response type — the printer has
 * no business knowing about confidence bands or OCR tokens.
 */
data class ConfirmedScan(
    val scanCode: String,
    val fields: Map<String, String?>,
    /**
     * Built by the backend, which owns the payload format so the app and server
     * cannot drift apart on it.
     */
    val qrPayload: String,
)

sealed interface PrintResult {

    data object Success : PrintResult

    /**
     * @param retryable false for configuration problems that will fail again
     *   identically until the operator changes something.
     */
    data class Failure(
        val reason: Reason,
        val message: String,
        val retryable: Boolean = true,
    ) : PrintResult

    enum class Reason {
        NOT_CONFIGURED,
        PERMISSION_DENIED,
        BLUETOOTH_OFF,
        NOT_PAIRED,
        NOT_READY,
        CONNECT_FAILED,
        WRITE_FAILED,
        PAYLOAD_TOO_LARGE,
    }
}
