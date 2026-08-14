package com.markss.dmartocr.ui

import android.content.Context
import android.view.LayoutInflater
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.markss.dmartocr.R
import com.markss.dmartocr.data.AppPreferences
import com.markss.dmartocr.databinding.DialogSettingsBinding

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

        binding.serverInput.setText(AppPreferences.serverUrl)
        binding.printerInput.setText(AppPreferences.printerMac)

        val dialog = MaterialAlertDialogBuilder(context)
            .setTitle(R.string.settings_title)
            .setView(binding.root)
            .setPositiveButton(R.string.settings_save, null)
            .setNegativeButton(R.string.settings_cancel, null)
            .setBackground(
                androidx.core.content.ContextCompat.getDrawable(context, R.drawable.bg_dialog)
            )
            .create()

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
                dialog.dismiss()
                onSaved()
            }
        }

        dialog.show()
    }
}
