package com.markss.dmartocr.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Wire models, mirroring the FastAPI response shapes one-to-one.
 *
 * Field names match the backend's camelCase aliases exactly. If the backend
 * contract changes, it changes here and nowhere else.
 */

@Serializable
data class ScanResponse(
    val scanId: String,
    val status: String,
    val overallConfidence: Double? = null,
    val fields: Map<String, ExtractedField> = emptyMap(),
    val tokens: List<OcrToken> = emptyList(),
    val timings: Map<String, Int> = emptyMap(),
    val variantUsed: String? = null,
    /** False when OCR succeeded but the database write did not. */
    val persisted: Boolean = true,
    val message: String? = null,
) {
    val isCompleted: Boolean get() = status == STATUS_COMPLETED
    val isNoText: Boolean get() = status == STATUS_NO_TEXT

    companion object {
        const val STATUS_COMPLETED = "COMPLETED"
        const val STATUS_NO_TEXT = "NO_TEXT_DETECTED"
    }
}

@Serializable
data class ExtractedField(
    val value: String? = null,
    val confidence: Double = 0.0,
    val band: String = BAND_LOW,
    val source: String = SOURCE_NOT_FOUND,
    val rawValue: String? = null,
    /**
     * Human label supplied by the server, so a key this app has never seen
     * still renders readably. The field set is not fixed.
     */
    val displayName: String? = null,
    /** True for the core fields the POC always asks about. */
    val expected: Boolean = false,
) {
    /** Derived by a rule printed on the pack rather than read from it. */
    val isDerived: Boolean get() = source == SOURCE_DERIVED

    val wasFound: Boolean get() = value != null

    companion object {
        const val BAND_HIGH = "HIGH"
        const val BAND_REVIEW = "REVIEW"
        const val BAND_LOW = "LOW"

        const val SOURCE_OCR = "OCR_RULES"
        const val SOURCE_DERIVED = "DERIVED_RULE"
        const val SOURCE_NOT_FOUND = "NOT_FOUND"
        const val SOURCE_OPERATOR = "OPERATOR"
    }
}

@Serializable
data class OcrToken(
    val text: String,
    val bbox: BoundingBox,
    val confidence: Double,
    val variant: String? = null,
)

@Serializable
data class BoundingBox(
    val x: Int,
    val y: Int,
    val width: Int,
    val height: Int,
)

@Serializable
data class ConfirmRequest(
    val fields: Map<String, String?>,
    @SerialName("deviceId") val deviceId: String? = null,
)

@Serializable
data class ConfirmResponse(
    val scanId: String,
    val status: String,
    val persisted: Boolean = true,
    val validationNotes: Map<String, List<String>> = emptyMap(),
    val qrPayload: String? = null,
)

@Serializable
data class PrintResponse(
    val scanId: String,
    val status: String,
    val qrPayload: String? = null,
)

@Serializable
data class HealthResponse(
    val status: String,
    val ocrReady: Boolean = false,
    val detectionModel: String? = null,
    val recognitionModel: String? = null,
)
