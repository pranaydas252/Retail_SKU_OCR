package com.markss.dmartocr.ocr

import android.graphics.Rect
import android.graphics.RectF
import com.google.mlkit.vision.text.Text

/**
 * What ML Kit can tell us about a frame before it is captured.
 *
 * ML Kit reads a full-resolution capture in about 250ms on a TC22, which is
 * fast enough to run on the preview stream. That does not make it a better
 * recogniser than the server pipeline — measured on the sample captures it
 * reaches 29% on the core fields against PP-OCRv5's 43% — but it makes it a
 * very good judge of whether a frame is worth capturing at all.
 *
 * This matters more than it sounds. Every accuracy figure in this project is
 * measured on frames an operator already chose to keep, and nothing in the
 * system checks whether that frame was worth keeping. Rejecting a blurred or
 * badly framed capture before it is taken is upstream of every recognition
 * improvement.
 */
data class LabelQuality(
    /** Lines of text detected inside the ROI window. */
    val linesInRoi: Int,
    /** True when at least one of them looks like a field label. */
    val hasFieldLabel: Boolean,
    /** Median rotation of the detected lines, in degrees. */
    val skewDegrees: Float,
    /** Mean ML Kit confidence over lines inside the ROI. */
    val confidence: Float,
) {
    /**
     * Whether this frame is worth capturing.
     *
     * Deliberately forgiving. A gate that blocks capture is worse than a bad
     * capture if it ever refuses a label the operator can plainly see — they
     * cannot argue with it, and the scan simply cannot proceed. So it asks for
     * evidence that a declaration panel is present, not that the read is good.
     */
    val isReady: Boolean
        get() = linesInRoi >= MIN_LINES && kotlin.math.abs(skewDegrees) <= MAX_SKEW

    /** Operator-facing reason the frame is not ready yet, or null when it is. */
    fun hint(): Int? = when {
        linesInRoi == 0 -> R_NO_TEXT
        linesInRoi < MIN_LINES -> R_MOVE_CLOSER
        kotlin.math.abs(skewDegrees) > MAX_SKEW -> R_STRAIGHTEN
        else -> null
    }

    companion object {
        /**
         * A declaration panel is several printed rows. One stray line is a
         * brand name or a barcode number caught at the edge of the window.
         */
        const val MIN_LINES = 3

        /**
         * Beyond this the frame is deskewed rather than refused — the operator
         * is told to straighten only when rotation is severe enough that the
         * correction itself would degrade the image.
         */
        const val MAX_SKEW = 35f

        // Resource ids are passed through rather than resolved here, so this
        // class stays free of Android context and is unit-testable.
        const val R_NO_TEXT = 1
        const val R_MOVE_CLOSER = 2
        const val R_STRAIGHTEN = 3

        /**
         * Field labels, matched loosely.
         *
         * Deliberately not the server's full alias table. This only has to
         * answer "does this look like a declaration panel", and the recogniser
         * mangles labels on preview frames — it returned "Mig. Date" for
         * "Mfg. Date" on a real capture — so a strict list would reject good
         * frames.
         */
        private val LABEL_HINTS = listOf(
            "MRP", "BATCH", "LOT", "MFG", "MFD", "EXP", "USE BY", "USEBY",
            "BEST BEFORE", "PKD", "PACK", "PRICE", "DATE",
        )

        /**
         * Scores a recognition result against the ROI window.
         *
         * @param roi the ROI in image pixel coordinates, not view coordinates
         */
        fun of(text: Text, roi: Rect): LabelQuality {
            val lines = text.textBlocks
                .flatMap { it.lines }
                .filter { line -> line.boundingBox?.let { Rect.intersects(it, roi) } == true }

            if (lines.isEmpty()) {
                return LabelQuality(0, hasFieldLabel = false, skewDegrees = 0f, confidence = 0f)
            }

            val angles = lines.map { it.angle }.sorted()
            val median = angles[angles.size / 2]

            val hasLabel = lines.any { line ->
                val upper = line.text.uppercase()
                LABEL_HINTS.any { upper.contains(it) }
            }

            return LabelQuality(
                linesInRoi = lines.size,
                hasFieldLabel = hasLabel,
                skewDegrees = median,
                // Confidence is documented as unavailable on some builds, where
                // it reads 0. Averaging it in regardless would make every frame
                // on such a device look unreadable, so it is reported but not
                // used by isReady.
                confidence = lines.map { it.confidence }.average().toFloat(),
            )
        }

        /** Converts an ROI expressed as view fractions into image pixels. */
        fun roiInImage(roi: RectF, width: Int, height: Int): Rect = Rect(
            (roi.left * width).toInt().coerceIn(0, width - 1),
            (roi.top * height).toInt().coerceIn(0, height - 1),
            (roi.right * width).toInt().coerceIn(1, width),
            (roi.bottom * height).toInt().coerceIn(1, height),
        )
    }
}
