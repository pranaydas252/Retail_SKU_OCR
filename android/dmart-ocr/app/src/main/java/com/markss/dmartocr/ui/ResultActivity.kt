package com.markss.dmartocr.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.markss.dmartocr.R
import com.markss.dmartocr.data.ApiClient
import com.markss.dmartocr.data.ConfirmRequest
import com.markss.dmartocr.data.ExtractedField
import com.markss.dmartocr.data.ScanResponse
import com.markss.dmartocr.databinding.ActivityResultBinding
import com.markss.dmartocr.databinding.DialogAddFieldBinding
import com.markss.dmartocr.databinding.ItemFieldBinding
import com.markss.dmartocr.device.DeviceId
import com.markss.dmartocr.print.BluetoothLabelPrinter
import com.markss.dmartocr.print.ConfirmedScan
import com.markss.dmartocr.print.LabelPrinter
import com.markss.dmartocr.print.PrintResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json

/**
 * Confirmation screen.
 *
 * The operator is the final authority before anything is committed (CLAUDE.md
 * section 4). Every field is editable, including ones OCR never found, and
 * nothing is submitted until they press confirm.
 */
class ResultActivity : AppCompatActivity() {

    private lateinit var binding: ActivityResultBinding
    private lateinit var scan: ScanResponse

    /** Field name to its input row, in display order. */
    private val rows = linkedMapOf<String, ItemFieldBinding>()

    /** Behind the interface so a different transport never touches this screen. */
    private val printer: LabelPrinter by lazy { BluetoothLabelPrinter(this) }

    /** Set while a print is waiting on the Bluetooth permission prompt. */
    private var pendingPrint: Pair<Map<String, String?>, String>? = null

    private val requestBluetooth = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val pending = pendingPrint
        pendingPrint = null
        if (pending == null) return@registerForActivityResult

        if (grants.values.all { it }) {
            printLabel(pending.first, pending.second)
            return@registerForActivityResult
        }

        setBusy(false)

        // "Don't ask again" leaves shouldShowRequestPermissionRationale false
        // for a permission the operator never granted. Re-prompting then does
        // nothing at all, so the only honest next step is Settings.
        val permanentlyDenied = grants.filterValues { !it }.keys.none {
            ActivityCompat.shouldShowRequestPermissionRationale(this, it)
        }

        showPermissionDenied(pending.first, pending.second, permanentlyDenied)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityResultBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val payload = intent.getStringExtra(EXTRA_SCAN_JSON)
        if (payload == null) {
            finish()
            return
        }
        scan = decode(payload)

        binding.scanId.text = getString(R.string.result_scan_id, scan.scanId)
        binding.unsavedBanner.visibility = if (scan.persisted) View.GONE else View.VISIBLE

        buildFields()
        updateSummary()

        binding.addFieldButton.setOnClickListener { showAddFieldDialog() }
        binding.retakeButton.setOnClickListener { finish() }
        binding.confirmButton.setOnClickListener { confirm() }
    }

    /**
     * Renders every field the server returned, whatever its key.
     *
     * There is no hard-coded field list. The rule extractor emits five known
     * keys today, but a document-understanding model returns whatever a pack
     * actually prints, and a fixed list would silently drop anything new. The
     * server sends a displayName with each field so a key this app has never
     * seen still gets a readable label.
     */
    private fun buildFields() {
        // Worst first. The values most likely to be wrong must not sit below
        // the fold where a hurried operator will scroll past them.
        scan.fields.entries
            .sortedBy { (_, field) ->
                when {
                    !field.wasFound -> 0
                    field.band == ExtractedField.BAND_LOW -> 1
                    field.band == ExtractedField.BAND_REVIEW -> 2
                    else -> 3
                }
            }
            .forEach { (name, field) -> addRow(name, field) }
    }

    private fun addRow(name: String, field: ExtractedField, userAdded: Boolean = false) {
        val row = ItemFieldBinding.inflate(
            LayoutInflater.from(this), binding.fieldContainer, false
        )

        row.fieldLabel.text = field.displayName ?: humanise(name)
        row.fieldValue.setText(field.value ?: "")
        applyBand(row, field, userAdded)

        if (userAdded) {
            // Only rows the operator created can be removed. Deleting a field
            // the server returned would hide evidence rather than correct it;
            // clearing its value already says "not on this pack".
            row.removeButton.visibility = View.VISIBLE
            row.removeButton.setOnClickListener {
                binding.fieldContainer.removeView(row.root)
                rows.remove(name)
                updateSummary()
            }
        }

        // Editing invalidates the band: the server's confidence described the
        // value it read, not the one the operator just typed. Leaving a green
        // "clear" chip on an edited field would assert something the system no
        // longer knows.
        row.fieldValue.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) markEdited(row, field)
        }

        rows[name] = row
        binding.fieldContainer.addView(row.root)
    }

    /** Fallback label when the server sends a key without a display name. */
    private fun humanise(key: String): String {
        val spaced = key
            .replace(Regex("[_-]+"), " ")
            .replace(Regex("(?<=[a-z0-9])(?=[A-Z])"), " ")
            .trim()
        return spaced.replaceFirstChar { it.uppercase() }
    }

    private fun applyBand(
        row: ItemFieldBinding,
        field: ExtractedField,
        userAdded: Boolean = false,
    ) {
        if (userAdded) {
            // Operator-entered values carry no OCR confidence, so a band would
            // be meaningless. Marked as theirs instead.
            val added = ContextCompat.getColor(this, R.color.brand_cyan)
            row.bandRail.setBackgroundColor(added)
            row.bandChip.setBackgroundResource(R.drawable.bg_chip_neutral)
            row.bandChip.setTextColor(added)
            row.bandChip.setText(R.string.band_added)
            row.fieldNote.visibility = View.GONE
            return
        }

        val (colorRes, chipBg, chipText) = when {
            !field.wasFound -> Triple(
                R.color.band_low, R.drawable.bg_chip_low, R.string.band_low
            )

            field.band == ExtractedField.BAND_HIGH -> Triple(
                R.color.band_high, R.drawable.bg_chip_high, R.string.band_high
            )

            field.band == ExtractedField.BAND_REVIEW -> Triple(
                R.color.band_review, R.drawable.bg_chip_review, R.string.band_review
            )

            else -> Triple(R.color.band_low, R.drawable.bg_chip_low, R.string.band_low)
        }

        val color = ContextCompat.getColor(this, colorRes)
        row.bandRail.setBackgroundColor(color)
        row.bandChip.setBackgroundResource(chipBg)
        row.bandChip.setTextColor(color)
        row.bandChip.setText(chipText)

        // A derived expiry is not a reading — it is arithmetic on a shelf life
        // printed elsewhere on the pack. Say so, because the operator is being
        // asked to vouch for it.
        if (field.isDerived) {
            row.fieldNote.visibility = View.VISIBLE
            row.fieldNote.setText(R.string.result_derived)
            row.fieldNote.setTextColor(ContextCompat.getColor(this, R.color.band_review))
        } else {
            row.fieldNote.visibility = View.GONE
        }
    }

    private fun markEdited(row: ItemFieldBinding, original: ExtractedField) {
        val current = row.fieldValue.text?.toString()?.trim().orEmpty()
        val unchanged = current == (original.value ?: "")
        if (unchanged) return

        val color = ContextCompat.getColor(this, R.color.brand_cyan)
        row.bandRail.setBackgroundColor(color)
        row.bandChip.setBackgroundResource(R.drawable.bg_chip_neutral)
        row.bandChip.setTextColor(color)
        row.bandChip.text = getString(R.string.band_review)
        updateSummary()
    }

    private fun updateSummary() {
        val needingReview = scan.fields.count { (name, field) ->
            rows.containsKey(name) && (!field.wasFound || field.band != ExtractedField.BAND_HIGH)
        }

        if (needingReview == 0) {
            binding.summaryBanner.setBackgroundResource(R.drawable.bg_chip_high)
            binding.summaryIcon.setImageResource(R.drawable.ic_check)
            binding.summaryIcon.imageTintList =
                ContextCompat.getColorStateList(this, R.color.band_high)
            binding.summaryText.setText(R.string.result_all_clear)
            binding.summaryText.setTextColor(
                ContextCompat.getColor(this, R.color.band_high)
            )
        } else {
            val text = if (needingReview == 1) {
                getString(R.string.result_review_count, needingReview)
            } else {
                getString(R.string.result_review_count_plural, needingReview)
            }
            binding.summaryBanner.setBackgroundResource(R.drawable.bg_chip_review)
            binding.summaryIcon.setImageResource(R.drawable.ic_alert)
            binding.summaryIcon.imageTintList =
                ContextCompat.getColorStateList(this, R.color.band_review)
            binding.summaryText.text = text
            binding.summaryText.setTextColor(
                ContextCompat.getColor(this, R.color.band_review)
            )
        }
    }

    private fun showAddFieldDialog() {
        val dialogBinding = DialogAddFieldBinding.inflate(LayoutInflater.from(this))

        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.add_field_title)
            .setView(dialogBinding.root)
            .setPositiveButton(R.string.settings_save, null)
            .setNegativeButton(R.string.settings_cancel, null)
            .setBackground(ContextCompat.getDrawable(this, R.drawable.bg_dialog))
            .create()

        dialog.setOnShowListener {
            dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val label = dialogBinding.nameInput.text?.toString()?.trim().orEmpty()
                val value = dialogBinding.valueInput.text?.toString()?.trim().orEmpty()

                dialogBinding.nameLayout.error = null
                if (label.isEmpty()) {
                    dialogBinding.nameLayout.error = getString(R.string.add_field_name_error)
                    return@setOnClickListener
                }

                val key = toKey(label)
                if (rows.containsKey(key)) {
                    dialogBinding.nameLayout.error = getString(R.string.add_field_duplicate)
                    return@setOnClickListener
                }

                addRow(
                    key,
                    ExtractedField(
                        value = value.ifEmpty { null },
                        source = ExtractedField.SOURCE_OPERATOR,
                        displayName = label,
                    ),
                    userAdded = true,
                )
                dialog.dismiss()
                binding.scroll.post { binding.scroll.fullScroll(View.FOCUS_DOWN) }
            }
        }

        dialog.show()
    }

    /** "Net contents" becomes "netContents", matching server key style. */
    private fun toKey(label: String): String {
        val parts = label.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (parts.isEmpty()) return label
        return parts.first().lowercase() +
            parts.drop(1).joinToString("") { p -> p.replaceFirstChar { it.uppercase() } }
    }

    private fun confirm() {
        // Blank means "this label does not carry the field", which is real
        // information and is sent as an explicit null rather than omitted.
        val values: Map<String, String?> = rows.mapValues { (_, row) ->
            row.fieldValue.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        }

        setBusy(true)

        lifecycleScope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    ApiClient.api.confirmScan(
                        scanId = scan.scanId,
                        body = ConfirmRequest(
                            fields = values,
                            deviceId = DeviceId.of(this@ResultActivity),
                        ),
                    )
                }
            }

            result.fold(
                onSuccess = { response ->
                    // Persisted. Printing is a separate, retryable step from
                    // here on — a print failure must never roll back a
                    // confirmed scan (CLAUDE.md §18).
                    printLabel(values, response.qrPayload)
                },
                onFailure = { error ->
                    setBusy(false)
                    MaterialAlertDialogBuilder(this@ResultActivity)
                        .setMessage(error.message ?: getString(R.string.error_network))
                        .setPositiveButton(R.string.error_retry, null)
                        .show()
                },
            )
        }
    }

    /**
     * Prints the label, then records the print on the server.
     *
     * The scan is already committed by the time this runs. Every outcome here
     * leaves the data intact — a failure offers a retry and an exit, and taking
     * the exit is a legitimate choice, because the record is safe whether or
     * not a label ever comes out.
     */
    private fun printLabel(values: Map<String, String?>, qrPayload: String?) {
        if (qrPayload.isNullOrBlank()) {
            // No payload means the server did not produce one. Printing a
            // label with an empty QR would be worse than printing nothing.
            setBusy(false)
            finish()
            return
        }

        // Checked before any permission prompt. If no printer is configured or
        // the radio is off, the outcome is identical with or without the
        // permission, and asking first would make the operator grant something
        // only to be told it was pointless.
        printer.preflight()?.let { failure ->
            setBusy(false)
            showPrintFailure(failure, values, qrPayload)
            return
        }

        // Ask for whatever is still missing, at the moment it is needed rather
        // than up front on a screen that may never print. The printer owns the
        // list because it knows what the SDK actually calls.
        val missing = (printer as? BluetoothLabelPrinter)?.missingPermissions().orEmpty()
        if (missing.isNotEmpty()) {
            pendingPrint = values to qrPayload
            requestBluetooth.launch(missing.toTypedArray())
            return
        }

        binding.confirmButton.setText(R.string.result_printing)

        lifecycleScope.launch {
            val result = printer.print(
                ConfirmedScan(
                    scanCode = scan.scanId,
                    fields = values,
                    qrPayload = qrPayload,
                )
            )

            setBusy(false)

            when (result) {
                is PrintResult.Success -> {
                    // Recorded after the printer confirms, never before.
                    runCatching {
                        withContext(Dispatchers.IO) {
                            ApiClient.api.recordPrint(scan.scanId)
                        }
                    }.onFailure {
                        // The label exists; only the audit note failed. Not
                        // worth stopping the operator over.
                        Log.w(TAG, "Print succeeded but was not recorded", it)
                    }
                    finish()
                }

                is PrintResult.Failure -> showPrintFailure(result, values, qrPayload)
            }
        }
    }

    /**
     * A refused permission must offer a way forward.
     *
     * The scan is already saved, so continuing is safe — but an operator has no
     * way to know that a label needs a permission they denied, let alone where
     * to change it. Retry re-prompts; once "don't ask again" has been chosen,
     * only Settings can help, so that is what is offered.
     */
    private fun showPermissionDenied(
        values: Map<String, String?>,
        qrPayload: String,
        permanentlyDenied: Boolean,
    ) {
        val builder = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.print_permission_title)
            .setMessage(
                if (permanentlyDenied) R.string.print_permission_settings_body
                else R.string.print_permission_body
            )
            .setNegativeButton(R.string.print_skip) { _, _ -> finish() }

        if (permanentlyDenied) {
            builder.setPositiveButton(R.string.scan_permission_settings) { _, _ ->
                startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null),
                    )
                )
            }
        } else {
            builder.setPositiveButton(R.string.print_permission_grant) { _, _ ->
                setBusy(true)
                printLabel(values, qrPayload)
            }
        }

        builder.show()
    }

    private fun showPrintFailure(
        failure: PrintResult.Failure,
        values: Map<String, String?>,
        qrPayload: String,
    ) {
        val builder = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.print_failed_title)
            .setMessage(getString(R.string.print_failed_body, failure.message))
            .setNegativeButton(R.string.print_skip) { _, _ -> finish() }

        if (failure.retryable) {
            builder.setPositiveButton(R.string.error_retry) { _, _ ->
                setBusy(true)
                printLabel(values, qrPayload)
            }
        }

        builder.show()
    }

    private fun setBusy(busy: Boolean) {
        binding.confirmButton.isEnabled = !busy
        binding.retakeButton.isEnabled = !busy
        binding.confirmButton.setText(
            if (busy) R.string.result_saving else R.string.result_confirm
        )
    }


    companion object {
        private const val TAG = "ResultActivity"
        const val EXTRA_SCAN_JSON = "scan_json"

        private val json = Json {
            ignoreUnknownKeys = true
            explicitNulls = false
            encodeDefaults = true
        }


        // Serializers passed explicitly: inside a companion object the bare
        // call resolves to the member overload that expects a
        // SerializationStrategy rather than the reified extension.
        fun encode(response: ScanResponse): String =
            json.encodeToString(ScanResponse.serializer(), response)

        fun decode(payload: String): ScanResponse =
            json.decodeFromString(ScanResponse.serializer(), payload)
    }
}
