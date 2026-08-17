package com.markss.retailocr.ocr

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The readiness rules, exercised against values logged from a real TC22.
 *
 * [LabelQuality] deliberately takes no Android types so these run as plain JVM
 * tests — the alternative is an instrumented test, and getting one of those to
 * run cost several build cycles and produced no signal.
 */
class LabelQualityTest {

    private fun reading(
        lines: Int = 12,
        label: Boolean = true,
        skew: Float = 0f,
        confidence: Float = 0.7f,
    ) = LabelQuality(lines, label, skew, confidence)

    @Test
    fun `level label is ready`() {
        assertNull(reading().hint())
    }

    @Test
    fun `empty frame asks the operator to aim`() {
        assertEquals(LabelQuality.R_NO_TEXT, reading(lines = 0, label = false).hint())
    }

    @Test
    fun `one stray line is not a declaration panel`() {
        assertEquals(LabelQuality.R_MOVE_CLOSER, reading(lines = 1).hint())
    }

    @Test
    fun `text without a field name is the wrong part of the pack`() {
        // Enough lines, read confidently, but no MRP or BATCH in sight - an
        // ingredients block or a marketing paragraph.
        assertEquals(LabelQuality.R_AIM_AT_PANEL, reading(label = false).hint())
    }

    @Test
    fun `blurred text is refused even when there is plenty of it`() {
        // Logged range for frames a person would call unreadable.
        assertEquals(LabelQuality.R_HOLD_STEADY, reading(confidence = 0.40f).hint())
    }

    @Test
    fun `missing confidence does not block capture`() {
        // ML Kit documents confidence as unavailable on some builds, where it
        // reads zero. Treating that as "unreadable" would refuse every frame on
        // such a device, so it must not gate.
        assertNull(reading(confidence = 0f).hint())
    }

    @Test
    fun `genuine tilt asks the operator to straighten`() {
        assertEquals(LabelQuality.R_STRAIGHTEN, reading(skew = 30f).hint())
    }
}
