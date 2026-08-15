package com.markss.dmartocr.print

import kotlin.math.roundToInt

/**
 * Generates the ZPL II for the confirmed-scan label.
 *
 * Layout is a text block on the left and a QR code on the right. The QR is the
 * constrained element: CLAUDE.md section 17 fixes it at 10mm x 10mm and
 * requires it to carry the confirmed field data rather than just an identifier.
 *
 * ## The 10mm budget
 *
 * The ZQ320 head is 203 dpi = 8 dots/mm, so 10mm = 80 dots. At `^BQ`
 * magnification 2 a module is 2 dots (0.25mm), giving 40 modules to work with.
 * QR version 5 is 37 x 37 modules = 74 dots = 9.25mm, which fits; version 6 is
 * 41 modules = 82 dots and overflows. Version 5 at error correction M holds
 * roughly 154 alphanumeric characters, against a typical payload of 47.
 *
 * Magnification must not drop below 2 — a 1-dot module is not reliably
 * scannable on thermal media — and the 4-module quiet zone sits OUTSIDE the
 * 10mm symbol, so it is reserved separately in the layout.
 *
 * ## ZPL choices
 *
 * `~SD` sets darkness on an absolute 0-30 scale, so re-sending it per label is
 * idempotent. `^MD` is deliberately not used: it adjusts relative to the
 * current setting and would compound across a run.
 *
 * `^CI28` selects UTF-8. The payload is uppercased ASCII, but a product name
 * echoed onto the label may not be.
 */
object ZplBuilder {

    /** Print head resolution. Every dot value below derives from this. */
    private const val DPI = 203
    private const val MM_PER_INCH = 25.4f

    /** ZQ320 is a 3-inch printer; 72mm is the printable width. */
    private val PRINT_WIDTH_DOTS = mm(72f)

    /** QR geometry, fixed by section 17. */
    private const val QR_MAGNIFICATION = 2
    private const val QR_ERROR_CORRECTION = "M"
    private val QR_SIZE_DOTS = mm(10f)

    /**
     * 4 modules per side at 2 dots each. Outside the 10mm symbol, so it must be
     * reserved in the layout or the symbol butts against neighbouring ink and
     * stops scanning.
     */
    private val QR_QUIET_ZONE_DOTS = 4 * QR_MAGNIFICATION

    private val MARGIN_DOTS = mm(3f)
    private val TITLE_HEIGHT_DOTS = mm(4.5f)
    private val LABEL_TEXT_HEIGHT_DOTS = mm(2.6f)
    private val VALUE_TEXT_HEIGHT_DOTS = mm(3.2f)
    private val ROW_SPACING_DOTS = mm(4.6f)

    private const val DARKNESS = 20

    /**
     * Capacity of QR version 5 at error correction M, in alphanumeric mode.
     * Exceeding it pushes the symbol to version 6, which overflows the 10mm box.
     */
    const val MAX_QR_PAYLOAD_CHARS = 154

    /** One printed row of the label. */
    data class Row(val label: String, val value: String)

    /**
     * Builds a complete print job.
     *
     * @param qrPayload the payload from the backend's confirm response. The
     *   backend owns this format so the app and server cannot disagree on it.
     */
    fun label(scanCode: String, rows: List<Row>, qrPayload: String): String {
        require(qrPayload.length <= MAX_QR_PAYLOAD_CHARS) {
            "QR payload is ${qrPayload.length} chars; over $MAX_QR_PAYLOAD_CHARS it " +
                "exceeds QR version 5 and overflows the 10mm box"
        }

        val qrX = PRINT_WIDTH_DOTS - MARGIN_DOTS - QR_SIZE_DOTS - QR_QUIET_ZONE_DOTS
        val textWidth = qrX - MARGIN_DOTS - QR_QUIET_ZONE_DOTS

        val body = StringBuilder()
        var y = MARGIN_DOTS

        body.append("^CF0,$TITLE_HEIGHT_DOTS")
        body.append("^FO$MARGIN_DOTS,$y^FDD-MART^FS")
        y += TITLE_HEIGHT_DOTS + mm(1.5f)

        // Rule under the header. ^GB with a height of 0 draws a plain line.
        body.append("^FO$MARGIN_DOTS,$y^GB$textWidth,0,2^FS")
        y += mm(2.5f)

        val scanY = y
        body.append("^CF0,$LABEL_TEXT_HEIGHT_DOTS")
        body.append("^FO$MARGIN_DOTS,$scanY^FD${escape(scanCode)}^FS")
        y += ROW_SPACING_DOTS

        rows.forEach { row ->
            body.append("^CF0,$LABEL_TEXT_HEIGHT_DOTS")
            body.append("^FO$MARGIN_DOTS,$y^FD${escape(row.label)}^FS")
            body.append("^CF0,$VALUE_TEXT_HEIGHT_DOTS")
            body.append("^FO${MARGIN_DOTS + mm(22f)},${y - mm(0.4f)}^FD${escape(row.value)}^FS")
            y += ROW_SPACING_DOTS
        }

        // QR, top-aligned with the field block.
        //
        // ^BQ orientation,model,magnification
        // ^FD<errorCorrection><inputMode>,<data>  — A selects automatic input
        // mode, which picks alphanumeric for an uppercased payload and packs
        // two characters per 11 bits.
        body.append("^FO$qrX,$scanY")
        body.append("^BQN,2,$QR_MAGNIFICATION")
        body.append("^FD${QR_ERROR_CORRECTION}A,${escape(qrPayload)}^FS")

        val labelLength = maxOf(y + MARGIN_DOTS, scanY + QR_SIZE_DOTS + MARGIN_DOTS)

        return buildString {
            append("~SD$DARKNESS")
            append("^XA")
            append("^CI28")
            append("^PW$PRINT_WIDTH_DOTS")
            append("^LL$labelLength")
            append("^LH0,0")
            append(body)
            append("^XZ")
        }
    }

    /**
     * Minimal self-test label: a QR at the real 10mm geometry plus a caption.
     *
     * Uses the same magnification and error correction as a production label,
     * so if this scans the density budget is proven on actual media rather than
     * on arithmetic.
     */
    fun testLabel(): String = buildString {
        append("~SD$DARKNESS")
        append("^XA")
        append("^CI28")
        append("^PW$PRINT_WIDTH_DOTS")
        append("^LL").append(QR_SIZE_DOTS + MARGIN_DOTS * 2 + mm(6f))
        append("^LH0,0")
        append("^CF0,").append(TITLE_HEIGHT_DOTS)
        append("^FO$MARGIN_DOTS,$MARGIN_DOTS^FDPRINTER OK^FS")
        append("^CF0,").append(LABEL_TEXT_HEIGHT_DOTS)
        append("^FO$MARGIN_DOTS,").append(MARGIN_DOTS + TITLE_HEIGHT_DOTS + mm(2f))
        append("^FD10mm QR test^FS")
        val qrX = PRINT_WIDTH_DOTS - MARGIN_DOTS - QR_SIZE_DOTS - QR_QUIET_ZONE_DOTS
        append("^FO$qrX,$MARGIN_DOTS")
        append("^BQN,2,$QR_MAGNIFICATION")
        append("^FD${QR_ERROR_CORRECTION}A,PRINTER-TEST^FS")
        append("^XZ")
    }

    /**
     * Escapes the ZPL control characters.
     *
     * `^` and `~` start commands and `\` escapes; an unescaped one in a batch
     * code would silently truncate the field or corrupt the job.
     */
    private fun escape(value: String): String =
        value.replace("\\", "\\\\").replace("^", "\\5E").replace("~", "\\7E")

    private fun mm(millimetres: Float): Int =
        maxOf(1, (millimetres / MM_PER_INCH * DPI).roundToInt())
}
