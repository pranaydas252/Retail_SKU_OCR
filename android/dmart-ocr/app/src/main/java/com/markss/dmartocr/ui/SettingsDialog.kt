package com.markss.dmartocr.ui

import android.content.Context
import android.view.LayoutInflater
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.markss.dmartocr.R
import com.markss.dmartocr.data.AppPreferences
import android.view.View
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.lifecycleScope
import com.markss.dmartocr.databinding.DialogSettingsBinding
import com.markss.dmartocr.print.BluetoothLabelPrinter
import com.markss.dmartocr.print.PrintResult
import kotlinx.coroutines.launch

/**
 * Site configuration: backend address and printer Bluetooth MAC.
 *
 * A dialog rather than a settings screen. There are two values, they are set
 * once per device at install time, and a whole screen for them would add a
 * navigation destination to an app that deliberately has almost none.
 */
object SettingsDialog {

    fun show(context: Context, onSaved: () -> Unit) {
        val binding = DialogSettingsBinding.inflate(LayoutInflater.from(context))
        val scope = (context as LifecycleOwner).lifecycleScope

        binding.serverInput.setText(AppPreferences.serverUrl)
        binding.printerInput.setText(AppPreferences.printerMac)
        binding.sampleModeSwitch.isChecked = AppPreferences.sampleMode

        val dialog = MaterialAlertDialogBuilder(context)
            .setTitle(R.string.settings_title)
            .setView(binding.root)
            .setPositiveButton(R.string.settings_save, null)
            .setNegativeButton(R.string.settings_cancel, null)
            .setBackground(
                androidx.core.content.ContextCompat.getDrawable(context, R.drawable.bg_dialog)
            )
            .create()

        // Proving the printer from here decouples two subsystems: diagnosing a
        // print failure by repeatedly photographing a product label confounds
        // OCR problems with printer problems.
        binding.testPrintButton.setOnClickListener {
            val mac = binding.printerInput.text?.toString().orEmpty()
            if (!AppPreferences.isValidMac(mac) || mac.isBlank()) {
                binding.printerLayout.error = context.getString(R.string.settings_printer_error)
                return@setOnClickListener
            }
            // Saved first so the printer uses what is on screen, not a stale value.
            AppPreferences.printerMac = mac

            binding.testPrintButton.isEnabled = false
            binding.testPrintResult.visibility = View.VISIBLE
            binding.testPrintResult.setText(R.string.settings_test_print_running)
            binding.testPrintResult.setTextColor(
                ContextCompat.getColor(context, R.color.text_secondary)
            )

            scope.launch {
                val result = BluetoothLabelPrinter(context).testPrint()
                binding.testPrintButton.isEnabled = true
                when (result) {
                    is PrintResult.Success -> {
                        binding.testPrintResult.setText(R.string.settings_test_print_ok)
                        binding.testPrintResult.setTextColor(
                            ContextCompat.getColor(context, R.color.band_high)
                        )
                    }
                    is PrintResult.Failure -> {
                        binding.testPrintResult.text = result.message
                        binding.testPrintResult.setTextColor(
                            ContextCompat.getColor(context, R.color.band_low)
                        )
                    }
                }
            }
        }

        dialog.setOnShowListener {
            // Bound after show() so a validation failure can keep the dialog
            // open. Wiring it through setPositiveButton would dismiss first and
            // silently discard bad input.
            dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val server = binding.serverInput.text?.toString().orEmpty()
                val mac = binding.printerInput.text?.toString().orEmpty()

                binding.serverLayout.error = null
                binding.printerLayout.error = null

                var valid = true
                if (server.isBlank() || !AppPreferences.isValidUrl(server)) {
                    binding.serverLayout.error = context.getString(R.string.settings_server_error)
                    valid = false
                }
                if (!AppPreferences.isValidMac(mac)) {
                    binding.printerLayout.error = context.getString(R.string.settings_printer_error)
                    valid = false
                }
                if (!valid) return@setOnClickListener

                AppPreferences.serverUrl = server
                AppPreferences.printerMac = mac
                AppPreferences.sampleMode = binding.sampleModeSwitch.isChecked
                dialog.dismiss()
                onSaved()
            }
        }

        dialog.show()
    }
}
