package com.markss.retailocr.ocr

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The behaviours that made the live indicator usable, pinned down.
 *
 * Each test here corresponds to something that was wrong on the device: advice
 * that changed faster than a person could act on, and a "ready" state that
 * disappeared before the shutter could be pressed.
 */
class ReadinessTrackerTest {

    private fun good() = LabelQuality(
        linesInRoi = 8, hasFieldLabel = true, skewDegrees = 2f, confidence = 0.72f
    )

    private fun poor() = LabelQuality(
        linesInRoi = 1, hasFieldLabel = false, skewDegrees = 3f, confidence = 0.35f
    )

    /** Feeds a reading repeatedly at a realistic frame interval. */
    private fun settle(
        tracker: ReadinessTracker,
        reading: LabelQuality,
        frames: Int,
        startAt: Long = 0L,
    ): Long {
        var now = startAt
        repeat(frames) {
            now += FRAME_MS
            tracker.update(reading, now)
        }
        return now
    }

    @Test
    fun `sustained good framing reaches green`() {
        val tracker = ReadinessTracker()
        settle(tracker, good(), 8)
        assertEquals(ReadinessTracker.Band.GOOD, tracker.band)
    }

    @Test
    fun `a single bad frame does not drop out of green`() {
        // The complaint verbatim: the offer vanished before it could be taken.
        val tracker = ReadinessTracker()
        var now = settle(tracker, good(), 8)

        now += FRAME_MS
        tracker.update(poor(), now)

        assertEquals(ReadinessTracker.Band.GOOD, tracker.band)
    }

    @Test
    fun `green survives briefly after the label leaves the frame`() {
        val tracker = ReadinessTracker()
        var now = settle(tracker, good(), 8)

        // Sustained loss, but still inside the hold window.
        repeat(3) {
            now += FRAME_MS
            tracker.update(poor(), now)
        }
        assertEquals(ReadinessTracker.Band.GOOD, tracker.band)
    }

    @Test
    fun `green does eventually drop when the label is gone`() {
        val tracker = ReadinessTracker()
        var now = settle(tracker, good(), 8)

        // Long enough for the smoothed score to fall through the exit
        // threshold. The first poor frames still read as green — that is the
        // smoothing doing its job — and each one refreshes the hold, so the
        // window only starts running once the score has actually decayed.
        now = settle(tracker, poor(), 8, now)

        now += ReadinessTracker.GREEN_HOLD_MS + FRAME_MS
        tracker.update(poor(), now)

        assertTrue(
            "expected to leave green, score=${tracker.score} band=${tracker.band}",
            tracker.band != ReadinessTracker.Band.GOOD,
        )
    }

    @Test
    fun `auto capture waits for the frame to hold`() {
        val tracker = ReadinessTracker()
        var now = 0L

        // One good frame must not fire the shutter - the camera sweeping past a
        // label on its way somewhere else would trigger a scan nobody asked for.
        now += FRAME_MS
        tracker.update(good(), now)
        assertFalse(tracker.shouldAutoCapture(now))

        now = settle(tracker, good(), 12, now)
        assertTrue(tracker.shouldAutoCapture(now))
    }

    @Test
    fun `reset re-arms nothing`() {
        val tracker = ReadinessTracker()
        val now = settle(tracker, good(), 12)
        assertTrue(tracker.shouldAutoCapture(now))

        tracker.reset()

        assertEquals(ReadinessTracker.Band.POOR, tracker.band)
        assertFalse(tracker.shouldAutoCapture(now))
    }

    private companion object {
        /** Recognition runs at roughly five frames a second on a TC22. */
        const val FRAME_MS = 200L
    }
}
