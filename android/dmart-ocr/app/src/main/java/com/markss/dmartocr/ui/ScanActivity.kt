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
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCaseGroup
import androidx.camera.core.ViewPort
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

        if (AppPreferences.sampleMode) {
            binding.instruction.setText(R.string.sample_mode_banner)
            binding.instructionHint.text =
                getString(R.string.sample_mode_hint, SampleStore.count(this))
        }

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
        cameraExecutor.shutdown()
    }

    /** Keeps the instruction text tied to the window it refers to. */
    private fun positionInstructionAboveRoi() {
        binding.roiOverlay.post {
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

            val useCaseGroup = UseCaseGroup.Builder()
                .addUseCase(preview)
                .addUseCase(imageCapture!!)
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

    private fun capture() {
        val capture = imageCapture ?: return
        if (capturing) return
        capturing = true

        showProcessing(true)

        // Read the ROI on the main thread and hand it to the callback. The
        // capture callback runs on a background executor, and reaching into a
        // View from there to read its geometry would be a data race.
        val roi = binding.roiOverlay.roiFraction()

        capture.takePicture(
            cameraExecutor,
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: ImageProxy) {
                    val bytes = try {
                        cropToRoi(image, roi)
                    } catch (e: Exception) {
                        Log.e(TAG, "Crop failed", e)
                        null
                    } finally {
                        image.close()
                    }

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
    private fun cropToRoi(image: ImageProxy, roi: RectF): ByteArray {
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

        val cropped = Bitmap.createBitmap(bitmap, left, top, right - left, bottom - top)

        Log.d(
            TAG,
            "Captured ${bitmap.width}x${bitmap.height}, " +
                "cropped to ${cropped.width}x${cropped.height} (rotation $rotation)"
        )

        return ByteArrayOutputStream().use { out ->
            // Quality 92: label text is small and JPEG ringing around thin
            // strokes costs OCR accuracy directly. The upload is a crop, not a
            // full frame, so the size is affordable.
            cropped.compress(Bitmap.CompressFormat.JPEG, 92, out)
            out.toByteArray()
        }
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

        if (total == null) {
            failed(getString(R.string.sample_save_failed))
            return
        }

        Toast.makeText(
            this,
            getString(R.string.sample_saved, total),
            Toast.LENGTH_SHORT,
        ).show()
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
    }
}
