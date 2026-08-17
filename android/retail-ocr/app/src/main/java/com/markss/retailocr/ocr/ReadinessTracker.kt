package com.markss.retailocr.ocr

/**
 * Turns a stream of noisy per-frame readings into something an operator can act on.
 *
 * Recognition runs about five times a second and each frame disagrees slightly
 * with the last, so the raw signal flickers. Shown directly it produced two
 * complaints on the device, and both are really the same one: the advice
 * changed faster than a person could respond, and "ready to capture" appeared
 * and vanished before the shutter could be pressed.
 *
 * Three mechanisms, each fixing a distinct part of that:
 *
 *  * a moving average, so one bad frame does not undo a good reading;
 *  * hysteresis, so a score sitting on the boundary does not oscillate between
 *    amber and green;
 *  * a hold, so green persists briefly after the last good frame — which is
 *    what makes it pressable, and is the difference between a gate that helps
 *    and one that taunts.
 *
 * The band arms the shutter; it never fires it. Those are two separate powers
 * and the split is the whole design (CLAUDE.md section 4).
 *
 * Arming is what the gate is for: a frame too poor to read costs a round trip
 * to the server and comes back as a failed scan, so refusing it here is
 * cheaper for the operator than letting it through.
 *
 * Firing belongs to the operator. An earlier version captured by itself once
 * the score had held green for a dwell period, reasoning that the frame knows
 * better than human reaction time when it is worth keeping. That was wrong
 * about who is in charge — the operator can see what the score cannot, and it
 * kept a motion-blurred frame nobody would have chosen (PLAN.md F3).
 */
class ReadinessTracker {

    private var smoothed: Float = 0f
    private var seenAny = false
    private var green = false
    private var lastGreenAt = 0L

    /** Smoothed score, 0..100. */
    val score: Int get() = smoothed.toInt()

    /** Current band, after smoothing and hysteresis. */
    var band: Band = Band.POOR
        private set

    enum class Band { POOR, FAIR, GOOD }

    /**
     * Feeds one reading in and returns the band to display.
     *
     * @param now monotonic milliseconds, passed in so this stays testable
     */
    fun update(reading: LabelQuality, now: Long): Band {
        val raw = reading.score.toFloat()

        // Seed on the first reading rather than easing up from zero, or the
        // display lags a second behind reality every time the screen opens.
        smoothed = if (!seenAny) {
            seenAny = true
            raw
        } else {
            SMOOTHING * raw + (1 - SMOOTHING) * smoothed
        }

        // Asymmetric thresholds. Entering green demands more than staying in
        // it, so a score hovering at the boundary settles instead of strobing.
        green = when {
            !green && smoothed >= ENTER_GOOD -> true
            green && smoothed < LEAVE_GOOD -> false
            else -> green
        }

        if (green) lastGreenAt = now

        // Hold green briefly after the last good frame. A hand shake should not
        // retract an offer the operator is already reaching for.
        val holding = lastGreenAt != 0L && now - lastGreenAt <= GREEN_HOLD_MS

        band = when {
            green || holding -> Band.GOOD
            smoothed >= ENTER_FAIR -> Band.FAIR
            else -> Band.POOR
        }
        return band
    }

    fun reset() {
        smoothed = 0f
        seenAny = false
        green = false
        lastGreenAt = 0L
        band = Band.POOR
    }

    companion object {
        /**
         * Weight of each new reading. At roughly five frames a second this
         * settles in about a second — fast enough to feel responsive, slow
         * enough that a single blurred frame does not move the band.
         */
        const val SMOOTHING = 0.35f

        const val ENTER_GOOD = 72f
        const val LEAVE_GOOD = 60f
        const val ENTER_FAIR = 40f

        /** How long green survives after the last good frame. */
        const val GREEN_HOLD_MS = 1200L
    }
}
