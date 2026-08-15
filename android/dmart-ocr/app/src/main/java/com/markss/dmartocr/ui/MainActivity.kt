package com.markss.dmartocr.ui

import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.markss.dmartocr.BuildConfig
import com.markss.dmartocr.R
import com.markss.dmartocr.data.ApiClient
import com.markss.dmartocr.databinding.ActivityMainBinding
import com.markss.dmartocr.device.ZebraGate
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Home screen.
 *
 * Runs the Zebra device gate on every resume — not just first launch — because
 * the EMDK runtime can be removed by an MDM push while the app sits in the
 * background.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var deviceAllowed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.settingsButton.setOnClickListener {
            SettingsDialog.show(this) {
                // Re-probe immediately: the operator changed the address
                // precisely because they think the old one was wrong.
                checkServer()
            }
        }

        if (BuildConfig.DEBUG) {
            // Debug-only: opens the confirmation screen with representative
            // data. The scan flow needs a readable label in front of the
            // camera, which makes iterating on this screen's layout slow and
            // makes verifying it before a release impossible. Never present in
            // a release build.
            binding.logo.setOnLongClickListener {
                startActivity(
                    Intent(this, ResultActivity::class.java).apply {
                        putExtra(ResultActivity.EXTRA_SCAN_JSON, ResultActivity.SAMPLE_JSON)
                    }
                )
                true
            }
        }

        binding.scanButton.setOnClickListener {
            if (deviceAllowed) {
                startActivity(Intent(this, ScanActivity::class.java))
            } else {
                showUnsupportedDeviceDialog()
            }
        }
    }

    override fun onResume() {
        super.onResume()
        applyDeviceGate()
        checkServer()
    }

    /**
     * The gate has no chip on screen. A verified device is the normal case and
     * does not need announcing; a blocked one gets a modal that stops the flow,
     * which is the only outcome the operator can act on. The decision is
     * logged either way for a technician.
     */
    private fun applyDeviceGate() {
        val result = ZebraGate.check(this)
        deviceAllowed = result !is ZebraGate.Result.Blocked

        if (result is ZebraGate.Result.Blocked) {
            showUnsupportedDeviceDialog(result.device)
        }

        binding.scanButton.isEnabled = deviceAllowed
    }

    /**
     * Reports server reachability before the operator commits to a scan.
     *
     * `ocrReady` is surfaced separately from liveness because PaddleOCR
     * downloads its models on first run: a server that is up but still fetching
     * models would otherwise look identical to a wedged one.
     */
    private fun checkServer() {
        setServerChip(R.string.main_server_checking, R.color.text_secondary)

        lifecycleScope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) { ApiClient.api.health() }
            }

            result.fold(
                onSuccess = { health ->
                    if (health.ocrReady) {
                        setServerChip(R.string.main_server_ready, R.color.band_high)
                    } else {
                        setServerChip(R.string.main_server_preparing, R.color.band_review)
                    }
                },
                onFailure = {
                    setServerChip(R.string.main_server_unreachable, R.color.band_low)
                },
            )
        }
    }

    private fun setServerChip(textRes: Int, colorRes: Int) {
        val color = ContextCompat.getColor(this, colorRes)
        binding.serverStatus.setText(textRes)
        binding.serverStatus.setTextColor(color)
        binding.serverDot.background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(color)
        }
    }

    private fun showUnsupportedDeviceDialog(device: String? = null) {
        val detail = device?.let { "\n\n" + getString(R.string.unsupported_detail, it) } ?: ""
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.unsupported_title)
            .setMessage(getString(R.string.unsupported_body) + detail)
            .setPositiveButton(R.string.unsupported_close, null)
            .setCancelable(false)
            .show()
    }
}
