package com.markss.dmartocr.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.graphics.RectF
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.util.Size
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCaseGroup
import androidx.camera.core.ViewPort
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.constraintlayout.widget.ConstraintLayout
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.markss.dmartocr.R
import com.markss.dmartocr.data.ApiClient
import com.markss.dmartocr.data.AppPreferences
import com.markss.dmartocr.data.SampleStore
import com.markss.dmartocr.data.ScanResponse
import com.markss.dmartocr.databinding.ActivityScanBinding
import com.markss.dmartocr.device.DeviceId
import com.markss.dmartocr.ocr.LabelAnalyzer
import com.markss.dmartocr.ocr.LabelQuality
import com.markss.dmartocr.ocr.ReadinessTracker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.roundToInt

/**
 * Camera screen.
 *
 * The operator aims the label into a fixed ROI window and presses capture. The
 * app crops to that window itself — there is no manual crop step, because that
 * would be a second task per scan for no benefit (CLAUDE.md section 4).
 */
class ScanActivity : AppCompatActivity() {

    private lateinit var binding: ActivityScanBinding
    private lateinit var cameraExecutor: ExecutorService

    private var imageCapture: ImageCapture? = null
    private var camera: androidx.camera.core.Camera? = null
    private var torchOn = false
    private var capturing = false

    private var analyzer: LabelAnalyzer? = null

    /**
     * Last readiness reading from the preview analyzer.
     *
     * Its skew is what the capture is deskewed by. Taking the angle from the
     * live stream rather than re-running recognition on the still keeps the
     * shutter responsive — the reading is at most a frame old, and a label does
     * not rotate between the last preview frame and the shutter.
     */
    @Volatile
    private var quality: LabelQuality? = null

    /** Smooths the per-frame readings into something stable enough to act on. */
    private val readiness = ReadinessTracker()

    /**
     * ROI window as view fractions, cached for the analyzer thread.
     *
     * The analyzer runs on the camera executor and must not touch a View to
     * read its geometry — the same race the capture path avoids by reading the
     * ROI on the main thread first. The rect is fixed once laid out, so a
     * single snapshot is enough.
     */
    @Volatile
    private var roiFraction: RectF = RectF(0f, 0f, 1f, 1f)

    private val requestCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera() else showPermissionDialog()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        setTheme(R.style.Theme_DmartOcr_Camera)
        super.onCreate(savedInstanceState)
        binding = ActivityScanBinding.inflate(layoutInflater)
        setContentView(binding.root)

        cameraExecutor = Executors.newSingleThreadExecutor()

        binding.closeButton.setOnClickListener { finish() }
        binding.torchButton.setOnClickListener { toggleTorch() }
        binding.captureButton.setOnClickListener { capture() }

        if (AppPreferences.sampleMode) showSampleCount()

        // Held shut until the framing earns it, in BOTH modes.
        //
        // Sample collection used to be exempt, on the reasoning that its
        // purpose is to gather difficult images including ones the gate would
        // reject. That was wrong, and the accuracy numbers show why: a corpus
        // collected without the gate is not the input the backend receives,
        // because in production the gate is what decides which frames exist at
        // all. Measuring a recognition engine against frames that could never
        // reach it produces a number describing nothing.
        //
        // So samples are captured exactly as scans are — same score, same
        // hysteresis, same auto-capture — and differ only in what happens
        // afterwards.
        setShutterEnabled(false)

        positionInstructionAboveRoi()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            requestCamera.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        analyzer?.close()
        cameraExecutor.shutdown()
    }

    /** Keeps the instruction text tied to the window it refers to. */
    private fun positionInstructionAboveRoi() {
        binding.roiOverlay.post {
            roiFraction = binding.roiOverlay.roiFraction()
            val roi = binding.roiOverlay.roiRect()
            val params = binding.instructionGroup.layoutParams as ConstraintLayout.LayoutParams
            val gap = (24 * resources.displayMetrics.density).toInt()
            params.topMargin = (roi.top.roundToInt() - gap - binding.instructionGroup.height)
                .coerceAtLeast(gap * 3)
            binding.instructionGroup.layoutParams = params
        }
    }

    private fun startCamera() {
        // Deferred until the preview has been laid out. ViewPort needs the
        // preview's aspect ratio, and a Rational built from a zero-width view
        // throws — the provider listener can otherwise fire before layout.
        binding.previewView.post { bindCamera() }
    }

    private fun bindCamera() {
        if (binding.previewView.width == 0 || binding.previewView.height == 0) {
            binding.previewView.post { bindCamera() }
            return
        }

        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = providerFuture.get()

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.previewView.surfaceProvider)
            }

            imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .setTargetRotation(binding.previewView.display?.rotation ?: 0)
                .build()

            // A shared ViewPort is what makes the ROI crop correct.
            //
            // Preview and capture otherwise cover different fields of view, and
            // applying preview-relative coordinates to the full-resolution
            // capture crops the wrong region — subtly wrong, which is worse
            // than obviously wrong because it reads as an OCR failure. Binding
            // both through one ViewPort guarantees the same visible area, so
            // the overlay's fractional rect describes the same physical region
            // in each (PLAN.md R14).
            val viewPort = ViewPort.Builder(
                android.util.Rational(binding.previewView.width, binding.previewView.height),
                imageCapture!!.targetRotation,
            ).setScaleType(ViewPort.FIT).build()

            // Readiness analysis shares the ViewPort with preview and capture,
            // so the ROI fractions address the same physical region in all
            // three. Without that the gate would judge a different area than
            // the one the operator is aiming at.
            //
            // The resolution is set explicitly, and that matters more than it
            // looks. ImageAnalysis defaults to 640x480, so the gate was judging
            // a frame far worse than the one it goes on to upload — on a real
            // pack it found five lines of text on one frame and one on the
            // next, which the smoothing then read as the operator waving the
            // device about. The label was not moving; the recogniser was
            // failing on half the frames.
            //
            // 1280x720 is affordable: the 252ms ML Kit measurement that
            // justified this whole approach was taken on FULL-resolution
            // captures, so 720p is comfortably inside a budget already proven
            // on device. Sizing it from the benchmark, rather than leaving the
            // default, is the correction — the number that justified the design
            // was never the number the design ran at.
            //
            val analysisResolution = ResolutionSelector.Builder()
                .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                .setResolutionStrategy(
                    ResolutionStrategy(
                        Size(1280, 720),
                        ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                    )
                )
                .build()

            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setTargetRotation(imageCapture!!.targetRotation)
                .setResolutionSelector(analysisResolution)
                .build()
                .also { useCase ->
                    val quality = LabelAnalyzer(
                        roiProvider = { roiFraction },
                        onResult = { runOnUiThread { onQuality(it) } },
                    )
                    analyzer = quality
                    useCase.setAnalyzer(cameraExecutor, quality)
                }

            val useCaseGroup = UseCaseGroup.Builder()
                .addUseCase(preview)
                .addUseCase(imageCapture!!)
                .addUseCase(analysis)
                .setViewPort(viewPort)
                .build()

            try {
                provider.unbindAll()
                camera = provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    useCaseGroup,
                )
            } catch (e: Exception) {
                Log.e(TAG, "Camera bind failed", e)
                toastAndFinish(getString(R.string.error_server))
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun toggleTorch() {
        val control = camera?.cameraControl ?: return
        torchOn = !torchOn
        control.enableTorch(torchOn)
        binding.torchIcon.alpha = if (torchOn) 1f else 0.6f
    }

    /**
     * Reflects the live readiness reading in the instruction line and shutter.
     *
     * The reading is smoothed before it is shown. Raw per-frame values change
     * about five times a second, which on the device meant the advice changed
     * faster than a person could act on it and "ready to capture" vanished
     * before the shutter could be pressed.
     */
    private fun onQuality(reading: LabelQuality) {
        quality = reading
        if (capturing) return

        val now = android.os.SystemClock.elapsedRealtime()
        val band = readiness.update(reading, now)

        binding.instructionHint.setText(
            when (reading.hint()) {
                LabelQuality.R_NO_TEXT -> R.string.scan_hint_no_text
                LabelQuality.R_MOVE_CLOSER -> R.string.scan_hint_move_closer
                LabelQuality.R_STRAIGHTEN -> R.string.scan_hint_straighten
                LabelQuality.R_AIM_AT_PANEL -> R.string.scan_hint_aim_panel
                LabelQuality.R_HOLD_STEADY -> R.string.scan_hint_hold_steady
                else -> R.string.scan_hint_ready
            }
        )
        binding.roiOverlay.setBand(band)
        // The hint carries the same state as the brackets, so the operator can
        // read the words without looking away from the label to check a corner.
        binding.instructionHint.backgroundTintList = ContextCompat.getColorStateList(
            this,
            when (band) {
                ReadinessTracker.Band.GOOD -> R.color.hint_good
                ReadinessTracker.Band.FAIR -> R.color.hint_fair
                ReadinessTracker.Band.POOR -> R.color.hint_poor
            },
        )
        setShutterEnabled(band == ReadinessTracker.Band.GOOD)

        // Fire by itself once the frame has held up for a moment. The instant a
        // frame is worth keeping is decided by the frame, not by how quickly
        // someone can reach the button — which is what made a good reading
        // unusable before.
        if (readiness.shouldAutoCapture(now)) capture()
    }

    private fun setShutterEnabled(enabled: Boolean) {
        if (binding.captureButton.isEnabled == enabled) return
        binding.captureButton.isEnabled = enabled
        binding.captureButton.alpha = if (enabled) 1f else 0.4f
    }

    private fun capture() {
        val capture = imageCapture ?: return
        if (capturing) return
        capturing = true

        // Stop analysing the moment the shutter fires. The frame is decided,
        // and letting readiness keep updating would rewrite the hint under a
        // capture that has already been taken.
        analyzer?.enabled = false
        showProcessing(true)

        // Read the ROI on the main thread and hand it to the callback. The
        // capture callback runs on a background executor, and reaching into a
        // View from there to read its geometry would be a data race.
        val roi = binding.roiOverlay.roiFraction()
        val skew = quality?.takeIf { it.linesInRoi > 0 }?.skewDegrees ?: 0f

        capture.takePicture(
            cameraExecutor,
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: ImageProxy) {
                    val prepared = try {
                        cropToRoi(image, roi, skew)
                    } catch (e: Exception) {
                        Log.e(TAG, "Crop failed", e)
                        null
                    } finally {
                        image.close()
                    }

                    if (prepared != null) {
                        runOnUiThread { freezePreview(prepared.first) }
                    }

                    val bytes = prepared?.second
                    when {
                        bytes == null ->
                            runOnUiThread { failed(getString(R.string.error_server)) }

                        AppPreferences.sampleMode ->
                            runOnUiThread { saveSample(bytes) }

                        else -> upload(bytes)
                    }
                }

                override fun onError(exception: ImageCaptureException) {
                    Log.e(TAG, "Capture failed", exception)
                    runOnUiThread { failed(getString(R.string.error_server)) }
                }
            },
        )
    }

    /**
     * Crops the captured frame to the ROI window and encodes it as JPEG.
     *
     * The ROI arrives as fractions of the overlay view. Because preview and
     * capture share a ViewPort, those fractions address the same region of the
     * captured image.
     */
    private fun cropToRoi(
        image: ImageProxy,
        roi: RectF,
        skewDegrees: Float,
    ): Pair<Bitmap, ByteArray> {
        val buffer = image.planes[0].buffer
        val raw = ByteArray(buffer.remaining()).also { buffer.get(it) }

        var bitmap = BitmapFactory.decodeByteArray(raw, 0, raw.size)
            ?: throw IllegalStateException("Could not decode captured frame")

        // Rotate into display orientation before cropping, so the ROI fractions
        // and the bitmap axes agree.
        val rotation = image.imageInfo.rotationDegrees
        if (rotation != 0) {
            val matrix = Matrix().apply { postRotate(rotation.toFloat()) }
            bitmap = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
        }

        val left = (roi.left * bitmap.width).roundToInt().coerceIn(0, bitmap.width - 1)
        val top = (roi.top * bitmap.height).roundToInt().coerceIn(0, bitmap.height - 1)
        val right = (roi.right * bitmap.width).roundToInt().coerceIn(left + 1, bitmap.width)
        val bottom = (roi.bottom * bitmap.height).roundToInt().coerceIn(top + 1, bitmap.height)

        var cropped = Bitmap.createBitmap(bitmap, left, top, right - left, bottom - top)

        // Deskew using the rotation ML Kit measured on the live preview.
        //
        // Cropping first and rotating second is deliberate: rotating the whole
        // frame would resample every pixel of a 12MP capture for the sake of a
        // small window. Below the threshold the rotation is skipped entirely —
        // resampling costs a little sharpness, and on text a degree or two of
        // tilt costs less than the interpolation would.
        if (kotlin.math.abs(skewDegrees) >= MIN_DESKEW_DEGREES) {
            val matrix = Matrix().apply { postRotate(-skewDegrees) }
            cropped = Bitmap.createBitmap(
                cropped, 0, 0, cropped.width, cropped.height, matrix, true
            )
        }

        Log.d(
            TAG,
            "Captured ${bitmap.width}x${bitmap.height}, " +
                "cropped to ${cropped.width}x${cropped.height} " +
                "(rotation $rotation, deskew ${"%.1f".format(-skewDegrees)})"
        )

        val bytes = ByteArrayOutputStream().use { out ->
            // Quality 92: label text is small and JPEG ringing around thin
            // strokes costs OCR accuracy directly. The upload is a crop, not a
            // full frame, so the size is affordable.
            cropped.compress(Bitmap.CompressFormat.JPEG, 92, out)
            out.toByteArray()
        }
        return cropped to bytes
    }

    /**
     * Shows the captured still over the live preview.
     *
     * The Preview use case keeps streaming after the shutter, so without this
     * the screen carries on moving while the scan uploads and the operator
     * cannot tell what was taken — or whether the shutter fired. The frame sent
     * to the server was always a still; only the display suggested otherwise.
     */
    private fun freezePreview(frame: Bitmap) {
        binding.capturedPreview.setImageBitmap(frame)
        binding.capturedPreview.visibility = View.VISIBLE
        binding.roiOverlay.visibility = View.GONE
    }

    private fun unfreezePreview() {
        binding.capturedPreview.visibility = View.GONE
        binding.capturedPreview.setImageDrawable(null)
        binding.roiOverlay.visibility = View.VISIBLE
        // Start the reading again from nothing. Carrying the old score over
        // would re-arm the shutter — and with auto-capture, immediately fire it
        // again on a frame nobody has looked at since the failure.
        readiness.reset()
        setShutterEnabled(false)
        analyzer?.enabled = true
    }

    /**
     * Stores the crop instead of uploading, and stays on the camera.
     *
     * Collection is a bulk task — the operator photographs a shelf of products
     * one after another — so returning to the viewfinder immediately is the
     * whole point. Routing each capture through the result screen would make
     * gathering thirty samples a thirty-step chore.
     */
    private fun saveSample(bytes: ByteArray) {
        val total = SampleStore.save(this, bytes)
        showProcessing(false)
        capturing = false
        unfreezePreview()

        if (total == null) {
            failed(getString(R.string.sample_save_failed))
            return
        }

        showSampleCount()
        Toast.makeText(
            this,
            getString(R.string.sample_saved, total),
            Toast.LENGTH_SHORT,
        ).show()
    }

    /**
     * Running total in the top line.
     *
     * The hint below it now carries the readiness state in both modes, which
     * is where the count used to live, and a count is what tells the operator
     * how far through a collection run they are.
     */
    private fun showSampleCount() {
        binding.instruction.text = getString(
            R.string.sample_mode_banner_count, SampleStore.count(this)
        )
    }

    private fun upload(bytes: ByteArray) {
        lifecycleScope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    ApiClient.api.createScan(
                        image = ApiClient.imagePart(bytes),
                        deviceId = ApiClient.textPart(DeviceId.of(this@ScanActivity)),
                        deviceModel = ApiClient.textPart("${Build.MANUFACTURER} ${Build.MODEL}"),
                    )
                }
            }

            result.fold(
                onSuccess = { handleResponse(it) },
                onFailure = { error ->
                    Log.e(TAG, "Upload failed", error)
                    failed(getString(R.string.error_network))
                },
            )
        }
    }

    private fun handleResponse(response: ScanResponse) {
        when {
            response.isNoText -> failed(getString(R.string.error_no_text))

            response.isCompleted -> {
                showProcessing(false)
                capturing = false
                startActivity(
                    Intent(this, ResultActivity::class.java).apply {
                        putExtra(ResultActivity.EXTRA_SCAN_JSON, ResultActivity.encode(response))
                    }
                )
                finish()
            }

            else -> failed(response.message ?: getString(R.string.error_server))
        }
    }

    private fun failed(message: String) {
        showProcessing(false)
        capturing = false
        // Back to the live preview: the operator's next action is to reframe
        // and try again, and a frozen still they cannot clear would block it.
        unfreezePreview()
        MaterialAlertDialogBuilder(this)
            .setMessage(message)
            .setPositiveButton(R.string.error_retry, null)
            .show()
    }

    private fun showProcessing(visible: Boolean) {
        binding.processingOverlay.visibility = if (visible) View.VISIBLE else View.GONE
    }

    private fun showPermissionDialog() {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.scan_permission_title)
            .setMessage(R.string.scan_permission_body)
            .setPositiveButton(R.string.scan_permission_settings) { _, _ ->
                startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null),
                    )
                )
                finish()
            }
            .setNegativeButton(R.string.scan_close) { _, _ -> finish() }
            .setCancelable(false)
            .show()
    }

    private fun toastAndFinish(message: String) {
        MaterialAlertDialogBuilder(this)
            .setMessage(message)
            .setPositiveButton(R.string.scan_close) { _, _ -> finish() }
            .show()
    }

    companion object {
        private const val TAG = "ScanActivity"

        /**
         * Below this, tilt is left alone. Rotation resamples every pixel, and
         * on small printed text that costs more sharpness than a degree or two
         * of tilt costs the recogniser.
         */
        private const val MIN_DESKEW_DEGREES = 2f
    }
}
