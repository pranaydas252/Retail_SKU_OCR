package com.markss.retailocr.ui

import android.content.Context
import android.view.LayoutInflater
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.markss.retailocr.R
import com.markss.retailocr.data.AppPreferences
import android.view.View
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.lifecycleScope
import com.markss.retailocr.databinding.DialogSettingsBinding
import com.markss.retailocr.print.BluetoothLabelPrinter
import com.markss.retailocr.print.PrintResult
import kotlinx.coroutines.launch

/**
 * Site configuration: backend address and printer Bluetooth MAC.
 *
 * A dialog rather than a settings screen. There are two values, they are set
 * once per device at install time, and a whole screen for them would add a
 * navigation destination to an app that deliberately has almost none.
 */
object SettingsDialog {

    /**
     * Prompts for the Bluetooth permissions the printer needs.
     *
     * @return true when a prompt was raised and the caller should stop; false
     *         when everything needed is already granted.
     *
     * Uses the platform request directly rather than an ActivityResultLauncher
     * because this is a dialog, and a launcher has to be registered before the
     * host reaches STARTED. The operator taps Test print again after granting,
     * which is acceptable on a configuration screen used once per device.
     */
    private fun requestBluetoothIfNeeded(
        context: Context,
        binding: DialogSettingsBinding,
    ): Boolean {
        val activity = context as? android.app.Activity ?: return false
        val missing = BluetoothLabelPrinter(context).missingPermissions()
        if (missing.isEmpty()) return false

        binding.testPrintResult.visibility = View.VISIBLE
        binding.testPrintResult.setTextColor(
            ContextCompat.getColor(context, R.color.text_secondary)
        )

        // "Don't ask again" leaves the rationale flag false for a permission
        // that was never granted, and requesting again then shows nothing at
        // all. Sending the operator to Settings is the only honest move.
        val blocked = missing.none {
            androidx.core.app.ActivityCompat.shouldShowRequestPermissionRationale(activity, it)
        }

        if (blocked && AppPreferences.bluetoothAsked) {
            binding.testPrintResult.setText(R.string.settings_bluetooth_blocked)
            context.startActivity(
                android.content.Intent(
                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.fromParts("package", context.packageName, null),
                )
            )
            return true
        }

        AppPreferences.bluetoothAsked = true
        binding.testPrintResult.setText(R.string.settings_bluetooth_requested)
        androidx.core.app.ActivityCompat.requestPermissions(
            activity, missing.toTypedArray(), REQUEST_BLUETOOTH
        )
        return true
    }

    private const val REQUEST_BLUETOOTH = 4021

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

            // Ask for Bluetooth before trying, rather than reporting afterwards
            // that it was missing. Test print used to print "Bluetooth
            // permission is required to print." and then do nothing — the
            // operator has no way to know that means a system dialog they were
            // never shown. ResultActivity has always prompted; this path did
            // not, and it is the one an installer reaches first.
            if (requestBluetoothIfNeeded(context, binding)) return@setOnClickListener

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
