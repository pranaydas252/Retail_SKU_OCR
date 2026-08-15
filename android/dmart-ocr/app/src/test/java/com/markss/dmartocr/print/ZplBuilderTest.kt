package com.markss.dmartocr.print

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * ZPL generation tests.
 *
 * The QR geometry is the part worth testing: it is fixed by CLAUDE.md §17 at
 * 10mm x 10mm, and getting it wrong produces a label that looks fine and does
 * not scan.
 */
class ZplBuilderTest {

    private val rows = listOf(
        ZplBuilder.Row("BATCH", "A23C91"),
        ZplBuilder.Row("MFG", "2026-07"),
        ZplBuilder.Row("EXP", "2028-06"),
    )

    private val payload = "SCAN-000047|A23C91|2026-07|2028-06||245.00"

    private fun build() = ZplBuilder.label("SCAN-000047", rows, payload)

    @Test
    fun `job is a complete ZPL block`() {
        val zpl = build()
        assertTrue("must open with darkness then ^XA", zpl.startsWith("~SD"))
        assertTrue(zpl.contains("^XA"))
        assertTrue("must terminate", zpl.endsWith("^XZ"))
    }

    @Test
    fun `uses UTF-8 encoding`() {
        assertTrue(build().contains("^CI28"))
    }

    @Test
    fun `qr uses magnification 2 and error correction M`() {
        val zpl = build()
        // Magnification below 2 gives a 1-dot module, which is not reliably
        // scannable on thermal media (§17).
        assertTrue("^BQN,2,2 expected", zpl.contains("^BQN,2,2"))
        assertTrue("error correction M, automatic input mode", zpl.contains("^FDMA,"))
    }

    @Test
    fun `qr carries the field data not just the scan id`() {
        val zpl = build()
        assertTrue("payload must be embedded", zpl.contains(payload))
        assertTrue(zpl.contains("A23C91"))
    }

    @Test
    fun `print width matches a 3 inch head at 203 dpi`() {
        // 72mm printable at 8 dots/mm = 576, rounded from the DPI arithmetic.
        assertTrue(build().contains("^PW575"))
    }

    @Test
    fun `payload at the capacity limit is accepted`() {
        val maxPayload = "A".repeat(ZplBuilder.MAX_QR_PAYLOAD_CHARS)
        ZplBuilder.label("SCAN-000001", rows, maxPayload)
    }

    @Test
    fun `payload over capacity is rejected rather than silently overflowing`() {
        // One character past version 5 pushes the symbol to version 6, which is
        // 41 modules = 82 dots = 10.25mm and breaks the 10mm box.
        val tooLong = "A".repeat(ZplBuilder.MAX_QR_PAYLOAD_CHARS + 1)
        try {
            ZplBuilder.label("SCAN-000001", rows, tooLong)
            fail("expected the oversized payload to be rejected")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("10mm"))
        }
    }

    @Test
    fun `control characters in field values are escaped`() {
        // An unescaped ^ or ~ in a batch code would truncate the field or
        // corrupt the whole job.
        val zpl = ZplBuilder.label(
            "SCAN-000001",
            listOf(ZplBuilder.Row("BATCH", "A^B~C")),
            payload,
        )
        assertTrue("caret must be escaped", zpl.contains("A\\5EB\\7EC"))
    }

    @Test
    fun `absent fields are omitted rather than printed blank`() {
        val zpl = ZplBuilder.label(
            "SCAN-000001",
            listOf(ZplBuilder.Row("BATCH", "A23C91")),
            payload,
        )
        assertTrue(zpl.contains("BATCH"))
        assertTrue("no LOT row when the pack has no lot code", !zpl.contains("^FDLOT^FS"))
    }

    @Test
    fun `header and scan code are printed`() {
        val zpl = build()
        assertTrue(zpl.contains("^FDD-MART^FS"))
        assertTrue(zpl.contains("SCAN-000047"))
    }

    @Test
    fun `qr sits inside the printable width with its quiet zone`() {
        val zpl = build()

        // ^FOx,y immediately preceding the ^BQ command.
        val qrOrigin = Regex("""\^FO(\d+),(\d+)\^BQN""").find(zpl)
            ?: error("QR field origin not found")
        val x = qrOrigin.groupValues[1].toInt()

        val printWidth = Regex("""\^PW(\d+)""").find(zpl)!!.groupValues[1].toInt()

        val qrDots = 80          // 10mm at 203 dpi
        val quietZone = 8        // 4 modules x 2 dots, outside the symbol

        assertTrue(
            "QR right edge ${x + qrDots + quietZone} exceeds print width $printWidth",
            x + qrDots + quietZone <= printWidth,
        )
    }

    @Test
    fun `label length covers the qr block`() {
        val zpl = build()
        val length = Regex("""\^LL(\d+)""").find(zpl)!!.groupValues[1].toInt()
        val qrY = Regex("""\^FO\d+,(\d+)\^BQN""").find(zpl)!!.groupValues[1].toInt()

        assertTrue("label must be long enough for the QR", length >= qrY + 80)
    }

    @Test
    fun `darkness is absolute so repeated jobs do not compound`() {
        // ~SD is absolute on a 0-30 scale. ^MD adjusts relatively and would
        // darken further with every label in a run.
        val zpl = build()
        assertTrue(zpl.contains("~SD"))
        assertEquals("^MD must not be used", false, zpl.contains("^MD"))
    }
}
