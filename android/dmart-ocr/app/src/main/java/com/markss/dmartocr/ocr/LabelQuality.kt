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
        get() = hint() == null

    /** Operator-facing reason the frame is not ready yet, or null when it is. */
    fun hint(): Int? = when {
        linesInRoi == 0 -> R_NO_TEXT
        linesInRoi < MIN_LINES -> R_MOVE_CLOSER
        kotlin.math.abs(skewDegrees) > MAX_SKEW -> R_STRAIGHTEN
        // A declaration panel names its fields. Line count alone said "ready to
        // capture" on a frame held sideways whose text was barely legible —
        // there were plenty of lines, they just were not the ones we need.
        // Requiring a field name is what separates the panel from the
        // ingredients block or a marketing paragraph.
        !hasFieldLabel -> R_AIM_AT_PANEL
        // How well it is being read, as opposed to how much of it there is.
        // This is the measurement that actually separates a usable frame from
        // an unusable one: logged on a TC22, frames a person would call
        // readable scored 0.60-0.73 and frames they would not scored
        // 0.31-0.46, with very little in between.
        confidence > 0f && confidence < MIN_CONFIDENCE -> R_HOLD_STEADY
        else -> null
    }

    companion object {
        /**
         * A declaration panel is several printed rows. One stray line is a
         * brand name or a barcode number caught at the edge of the window.
         */
        const val MIN_LINES = 3

        /**
         * Deviation from axis-aligned, after folding out whole quarter turns.
         * Below this the capture is silently deskewed; above it the correction
         * would resample enough to hurt, so the operator is asked instead.
         */
        const val MAX_SKEW = 20f

        /**
         * Mean ML Kit line confidence below which the frame is not worth
         * sending. Set from logged values on a TC22 rather than by feel:
         * readable frames clustered at 0.60-0.73 and unreadable ones at
         * 0.31-0.46, so the boundary sits in the empty space between.
         */
        const val MIN_CONFIDENCE = 0.55f

        // Resource ids are passed through rather than resolved here, so this
        // class stays free of Android context and is unit-testable.
        const val R_NO_TEXT = 1
        const val R_MOVE_CLOSER = 2
        const val R_STRAIGHTEN = 3
        const val R_AIM_AT_PANEL = 4
        const val R_HOLD_STEADY = 5

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
        /**
         * Angle of a line's baseline, from its corner points.
         *
         * Text.getAngle() is not used. It misreported both directions on real
         * frames — "straighten the label" on a bottle whose text was level, and
         * "ready to capture" on a frame held at ninety degrees — so its
         * semantics do not match "how far is this text from level", whatever
         * else they describe.
         *
         * Corner points are unambiguous: they come back in reading order, so
         * the vector from the first to the second runs along the baseline and
         * its inclination is the rotation, measured rather than reported.
         */
        /**
         * Reduces a measured angle to its deviation from the nearest axis.
         *
         * The analysis buffer is not in display orientation, so level text does
         * not measure as zero. On a TC22 held upright, every line of a level
         * label came back at about -88 degrees, and both the corner-point
         * measurement and ML Kit's own getAngle agreed on it — the reading was
         * right and the frame of reference was wrong. Treating -88 as tilt is
         * what told the operator to straighten a bottle that was already
         * straight.
         *
         * Folding onto the nearest multiple of 90 removes that offset without
         * hard-coding it, so the result holds whatever the sensor orientation
         * turns out to be on a given device. What remains is the only thing
         * worth acting on: how far the text sits from axis-aligned. A label
         * that is a whole quarter-turn round is still axis-aligned, and both
         * recognisers read it perfectly well.
         */
        private fun foldToNearestAxis(degrees: Float): Float {
            val quarterTurns = Math.round(degrees / 90f)
            return degrees - quarterTurns * 90f
        }

        private fun baselineAngle(line: Text.Line): Float? {
            val corners = line.cornerPoints ?: return null
            if (corners.size < 2) return null
            val dx = (corners[1].x - corners[0].x).toFloat()
            val dy = (corners[1].y - corners[0].y).toFloat()
            if (dx == 0f && dy == 0f) return null
            return Math.toDegrees(kotlin.math.atan2(dy, dx).toDouble()).toFloat()
        }

        fun of(text: Text, roi: Rect): LabelQuality {
            val lines = text.textBlocks
                .flatMap { it.lines }
                .filter { line -> line.boundingBox?.let { Rect.intersects(it, roi) } == true }

            if (lines.isEmpty()) {
                return LabelQuality(0, hasFieldLabel = false, skewDegrees = 0f, confidence = 0f)
            }

            // Median, not mean. Curved packaging bends the fine print near the
            // edges of a bottle while the declaration rows stay level, and one
            // strongly curved line of statutory text would drag a mean past the
            // threshold and refuse a perfectly good frame.
            val angles = lines.mapNotNull { baselineAngle(it) }.sorted()
            val median = if (angles.isEmpty()) 0f else angles[angles.size / 2]

            val hasLabel = lines.any { line ->
                val upper = line.text.uppercase()
                LABEL_HINTS.any { upper.contains(it) }
            }

            return LabelQuality(
                linesInRoi = lines.size,
                hasFieldLabel = hasLabel,
                skewDegrees = foldToNearestAxis(median),
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
