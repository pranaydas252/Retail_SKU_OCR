package com.markss.dmartocr.ui

import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
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

    private fun applyDeviceGate() {
        when (val result = ZebraGate.check(this)) {
            is ZebraGate.Result.Allowed -> {
                deviceAllowed = true
                setDeviceChip(
                    text = getString(R.string.main_device_ready),
                    color = R.color.band_high,
                    container = R.drawable.bg_chip_high,
                    icon = R.drawable.ic_check,
                )
            }

            is ZebraGate.Result.Bypassed -> {
                deviceAllowed = true
                setDeviceChip(
                    text = getString(R.string.main_device_debug),
                    color = R.color.band_review,
                    container = R.drawable.bg_chip_review,
                    icon = R.drawable.ic_info,
                )
            }

            is ZebraGate.Result.Blocked -> {
                deviceAllowed = false
                setDeviceChip(
                    text = getString(R.string.unsupported_title),
                    color = R.color.band_low,
                    container = R.drawable.bg_chip_low,
                    icon = R.drawable.ic_alert,
                )
                showUnsupportedDeviceDialog(result.device)
            }
        }

        binding.scanButton.isEnabled = deviceAllowed
    }

    private fun setDeviceChip(text: String, color: Int, container: Int, icon: Int) {
        binding.deviceStatus.text = text
        binding.deviceStatus.setTextColor(ContextCompat.getColor(this, color))
        binding.deviceChip.setBackgroundResource(container)
        binding.deviceIcon.setImageResource(icon)
        binding.deviceIcon.imageTintList =
            ContextCompat.getColorStateList(this, color)
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
        AlertDialog.Builder(this)
            .setTitle(R.string.unsupported_title)
            .setMessage(getString(R.string.unsupported_body) + detail)
            .setPositiveButton(R.string.unsupported_close, null)
            .setCancelable(false)
            .show()
    }
}
