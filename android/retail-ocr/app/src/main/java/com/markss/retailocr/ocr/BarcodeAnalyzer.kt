package com.markss.retailocr.ocr

import android.annotation.SuppressLint
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage

/**
 * Reads the pack's barcode off the preview stream.
 *
 * This runs before label capture, and the ordering is the point: the barcode
 * gives the SKU code, which is the only value in the whole scan that is read
 * rather than recognised. Every OCR field is a guess an operator has to check;
 * a decoded barcode either resolves or it does not.
 *
 * Backpressure works the same way as [LabelAnalyzer] — the proxy is closed only
 * when detection completes, so CameraX holds the next frame and at most one
 * detection is ever in flight.
 *
 * Unlike the readiness gate this analyzer is one-shot: the first barcode it
 * decodes wins, [enabled] goes false, and it stops looking. A barcode does not
 * need smoothing or hysteresis the way a quality score does, because a decode
 * is already a checksummed yes-or-no rather than a noisy measurement.
 */
class BarcodeAnalyzer(
    private val onBarcode: (String) -> Unit,
) : ImageAnalysis.Analyzer {

    // No format restriction. Indian retail packs carry EAN-13 most of the time
    // but not always, and narrowing the format list buys speed the preview does
    // not need while turning an unusual-but-valid pack into an unscannable one.
    private val scanner = BarcodeScanning.getClient()

    @Volatile
    var enabled: Boolean = true

    @SuppressLint("UnsafeOptInUsageError")
    override fun analyze(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null || !enabled) {
            proxy.close()
            return
        }

        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)

        scanner.process(input)
            .addOnSuccessListener { barcodes ->
                // Deliberately NOT restricted to the ROI window. The ROI exists
                // to keep surrounding packaging out of the OCR upload; a barcode
                // is decoded on device and costs nothing extra wherever it sits,
                // and on a carton it is rarely on the same face as the date
                // stamp the ROI is framed around.
                val value = barcodes.asSequence()
                    .filter { it.valueType == Barcode.TYPE_PRODUCT || it.valueType == Barcode.TYPE_TEXT }
                    .mapNotNull { it.rawValue }
                    .firstOrNull { it.isNotBlank() }
                    ?: barcodes.firstNotNullOfOrNull { it.rawValue?.takeIf(String::isNotBlank) }

                if (value != null && enabled) {
                    enabled = false
                    onBarcode(value)
                }
            }
            .addOnCompleteListener { proxy.close() }
    }

    fun close() = scanner.close()
}
