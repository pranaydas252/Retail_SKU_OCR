package com.markss.dmartocr.ocr

import android.annotation.SuppressLint
import android.graphics.RectF
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions

/**
 * Runs ML Kit over preview frames and reports whether the label is framed.
 *
 * Backpressure is the whole design here. Recognition takes roughly 250ms and
 * the preview delivers frames far faster than that, so the analyzer keeps the
 * newest frame and drops the rest; queuing them would put the readiness
 * indicator seconds behind what the operator is pointing at, which is worse
 * than no indicator.
 */
class LabelAnalyzer(
    private val roiProvider: () -> RectF,
    private val onResult: (LabelQuality) -> Unit,
) : ImageAnalysis.Analyzer {

    private val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)

    @Volatile
    var enabled: Boolean = true

    @SuppressLint("UnsafeOptInUsageError")
    override fun analyze(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null || !enabled) {
            proxy.close()
            return
        }

        val rotation = proxy.imageInfo.rotationDegrees
        val input = InputImage.fromMediaImage(media, rotation)

        // The ROI is expressed as fractions of the preview view, and the
        // analysis frame is rotated relative to the sensor, so the pixel extent
        // has to follow the rotation rather than the buffer's own dimensions.
        val uprightWidth = if (rotation % 180 == 0) proxy.width else proxy.height
        val uprightHeight = if (rotation % 180 == 0) proxy.height else proxy.width
        val roi = LabelQuality.roiInImage(roiProvider(), uprightWidth, uprightHeight)

        recognizer.process(input)
            .addOnSuccessListener { text ->
                val quality = LabelQuality.of(text, roi)
                if (android.util.Log.isLoggable(TAG, android.util.Log.DEBUG)) {
                    // Both angle sources, so a disagreement between what ML Kit
                    // reports and what the corner points measure is visible in
                    // a log rather than only as a wrong hint on screen.
                    val reported = text.textBlocks.flatMap { it.lines }
                        .joinToString { "%.0f".format(it.angle) }
                    android.util.Log.d(
                        TAG,
                        // The frame size is logged because it is the thing that
                        // was silently wrong: ImageAnalysis defaults to 640x480
                        // and the gate was scoring that while the upload was
                        // full resolution. A number on screen beats a default
                        // nobody checked.
                        "frame=${uprightWidth}x$uprightHeight " +
                            "lines=${quality.linesInRoi} label=${quality.hasFieldLabel} " +
                            "skew=%.1f conf=%.2f reportedAngles=[$reported]"
                                .format(quality.skewDegrees, quality.confidence)
                    )
                }
                onResult(quality)
            }
            .addOnFailureListener { onResult(LabelQuality(0, false, 0f, 0f)) }
            // Closing only when recognition finishes is what applies the
            // backpressure: CameraX will not deliver the next frame until this
            // proxy is released, so at most one recognition is ever in flight.
            .addOnCompleteListener { proxy.close() }
    }

    fun close() = recognizer.close()

    companion object {
        const val TAG = "LabelAnalyzer"
    }
}
