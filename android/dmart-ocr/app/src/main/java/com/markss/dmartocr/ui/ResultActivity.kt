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
import android.view.ViewGroup
import android.view.View
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.gridlayout.widget.GridLayout
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

        binding.unsavedBanner.visibility = if (scan.persisted) View.GONE else View.VISIBLE

        buildFields()
        updateSummary()

        binding.addFieldButton.setOnClickListener { showAddFieldDialog() }
        binding.retakeButton.setOnClickListener { finish() }
        binding.confirmButton.setOnClickListener { confirm() }
    }

    /**
     * Renders the fields that were actually read, whatever their keys.
     *
     * There is no hard-coded field list. The rule extractor emits five known
     * keys today, but a document-understanding model returns whatever a pack
     * actually prints, and a fixed list would silently drop anything new. The
     * server sends a displayName with each field so a key this app has never
     * seen still gets a readable label.
     *
     * Fields the server did not find are NOT rendered. A pouch that prints no
     * lot code produced an empty card demanding attention for something that
     * does not exist, and five cards on every scan regardless of the pack is
     * the static field list this screen was built to avoid. "Add field" is the
     * way back for anything genuinely on the pack that was missed, which is
     * what dynamic keys were for.
     *
     * The count still reaches the operator through the summary line, so a
     * missed field is reported without a card asking them to fill it in.
     */
    private fun buildFields() {
        // Worst first. The values most likely to be wrong must not sit below
        // the fold where a hurried operator will scroll past them.
        scan.fields.entries
            .filter { (_, field) -> field.wasFound }
            .sortedBy { (_, field) ->
                when (field.band) {
                    ExtractedField.BAND_LOW -> 0
                    ExtractedField.BAND_REVIEW -> 1
                    else -> 2
                }
            }
            .forEach { (name, field) -> addRow(name, field) }

        // Text was recognised but nothing was extracted from it. Without this
        // the screen is a title, an empty grid and a confirm button, which
        // reads as a broken app rather than as a failed read.
        binding.emptyState.visibility =
            if (rows.isEmpty()) View.VISIBLE else View.GONE
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
                // Removing the last row puts the screen back to an empty grid,
                // which needs the explanation again.
                binding.emptyState.visibility =
                    if (rows.isEmpty()) View.VISIBLE else View.GONE
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
        binding.fieldContainer.addView(row.root, cellParams())
    }

    /**
     * Half-width cell in the two-column grid.
     *
     * GridLayout children default to WRAP_CONTENT and no weight, which makes
     * every card size to its own text and leaves a ragged right edge. A column
     * weight with a zero base width is what makes the two columns equal.
     */
    private fun cellParams(): GridLayout.LayoutParams =
        GridLayout.LayoutParams().apply {
            width = 0
            height = ViewGroup.LayoutParams.WRAP_CONTENT
            columnSpec = GridLayout.spec(GridLayout.UNDEFINED, 1f)
            // Half the gutter each side, so the space between two cards
            // matches the space between rows.
            val h = (5 * resources.displayMetrics.density).toInt()
            val v = (5 * resources.displayMetrics.density).toInt()
            setMargins(h, v, h, v)
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
            row.bandDot.setBackgroundResource(R.drawable.dot_neutral)
            setNote(row, R.string.note_added, R.color.brand_cyan)
            return
        }

        val (dot, note, colour) = when {
            // Neutral, not red. Nothing was read, so there is no value to be
            // wrong about — and plenty of packs genuinely do not print a lot
            // code. Marking an honest absence as a fault trains the operator
            // to ignore the colour that matters.
            !field.wasFound -> Triple(
                R.drawable.dot_neutral, R.string.note_missing, R.color.text_tertiary
            )

            field.isDerived -> Triple(
                R.drawable.dot_review, R.string.note_derived, R.color.band_review
            )

            field.band == ExtractedField.BAND_HIGH -> Triple(
                R.drawable.dot_high, 0, R.color.band_high
            )

            field.band == ExtractedField.BAND_REVIEW -> Triple(
                R.drawable.dot_review, R.string.note_check, R.color.band_review
            )

            else -> Triple(R.drawable.dot_low, R.string.note_verify, R.color.band_low)
        }

        row.bandDot.setBackgroundResource(dot)
        setNote(row, note, colour)
    }

    /**
     * A clear field says nothing; a doubtful one says why in words.
     *
     * Keeping the quiet case quiet is what makes the noisy case readable — if
     * every card carried a status line, none of them would register.
     */
    private fun setNote(row: ItemFieldBinding, textRes: Int, colourRes: Int) {
        if (textRes == 0) {
            // INVISIBLE, not GONE: the line keeps its space so a cleanly read
            // field is exactly as tall as a doubtful one. Collapsing it made
            // card height a function of how well each field happened to be
            // read, which is not something the operator should have to look at.
            row.fieldNote.visibility = View.INVISIBLE
            row.fieldNote.text = null
            return
        }
        row.fieldNote.visibility = View.VISIBLE
        row.fieldNote.setText(textRes)
        row.fieldNote.setTextColor(ContextCompat.getColor(this, colourRes))
    }

    private fun markEdited(row: ItemFieldBinding, original: ExtractedField) {
        val current = row.fieldValue.text?.toString()?.trim().orEmpty()
        if (current == (original.value ?: "")) return

        // The server's confidence described the value it read, not the one the
        // operator just typed. Leaving the old band would assert something the
        // system no longer knows.
        row.bandDot.setBackgroundResource(R.drawable.dot_neutral)
        setNote(row, R.string.note_edited, R.color.brand_cyan)
        updateSummary()
    }

    /**
     * One line saying whether the operator can skim or must look carefully.
     *
     * "Needs review" and "not found" are counted apart, because they ask for
     * different things. A field that carries a doubtful value needs checking
     * against the pack. A field that was never read has nothing to check — the
     * operator either types it or, on a pack that does not print it at all,
     * leaves it alone. Counting the two together told an operator holding a
     * pouch with no lot code that three values needed review when one did.
     */
    private fun updateSummary() {
        val needingReview = scan.fields.count { (name, field) ->
            rows.containsKey(name) && field.band != ExtractedField.BAND_HIGH
        }
        // Counted from what the server returned, not from what is on screen:
        // these fields have no card, and the summary line is now the only
        // place they are reported at all.
        //
        // Excluding rows the operator has since added is the point — once they
        // have supplied the expiry themselves it is no longer missing, and a
        // banner still asking for it would be the same nagging empty card in
        // another form.
        val notFound = scan.fields.count { (name, field) ->
            !field.wasFound && !rows.containsKey(name)
        }

        val summary: String? = when {
            needingReview > 0 && notFound > 0 ->
                getString(R.string.result_review_and_missing, needingReview, notFound)

            needingReview == 1 -> getString(R.string.result_review_count, 1)
            needingReview > 1 ->
                getString(R.string.result_review_count_plural, needingReview)

            notFound == 1 -> getString(R.string.result_missing_count, 1)
            notFound > 1 -> getString(R.string.result_missing_count_plural, notFound)

            else -> null
        }

        if (summary == null) {
            binding.summaryBanner.setBackgroundResource(R.drawable.bg_chip_high)
            binding.summaryIcon.setImageResource(R.drawable.ic_check)
            binding.summaryIcon.imageTintList =
                ContextCompat.getColorStateList(this, R.color.band_high)
            binding.summaryText.setText(R.string.result_all_clear)
            binding.summaryText.setTextColor(
                ContextCompat.getColor(this, R.color.band_high)
            )
        } else {
            binding.summaryBanner.setBackgroundResource(R.drawable.bg_chip_review)
            binding.summaryIcon.setImageResource(R.drawable.ic_alert)
            binding.summaryIcon.imageTintList =
                ContextCompat.getColorStateList(this, R.color.band_review)
            binding.summaryText.text = summary
            binding.summaryText.setTextColor(
                ContextCompat.getColor(this, R.color.band_review)
            )
        }
    }

    private fun showAddFieldDialog() {
        val dialogBinding = DialogAddFieldBinding.inflate(LayoutInflater.from(this))

        // The exact server key for the chip the operator tapped, if any.
        // toKey() reconstructs a key from a typed label and is good enough for
        // a field nobody has seen before, but it must not be used where the
        // real key is already known — "Expiry" and "Expiry date" reconstruct
        // to different keys, and the server stores whichever it is told.
        var chosenKey: String? = null

        val missing = scan.fields
            .filterKeys { !rows.containsKey(it) }
            .filterValues { !it.wasFound }

        if (missing.isNotEmpty()) {
            dialogBinding.suggestionLabel.visibility = View.VISIBLE
            dialogBinding.suggestions.visibility = View.VISIBLE

            missing.forEach { (key, field) ->
                val label = field.displayName ?: humanise(key)
                val chip = com.google.android.material.chip.Chip(this).apply {
                    text = label
                    isCheckable = false
                    setOnClickListener {
                        chosenKey = key
                        dialogBinding.nameInput.setText(label)
                        dialogBinding.nameLayout.error = null
                        dialogBinding.valueInput.requestFocus()
                    }
                }
                dialogBinding.suggestions.addView(chip)
            }
        }

        // Typing over a suggestion means the operator no longer wants the key
        // that came with it.
        dialogBinding.nameInput.setOnKeyListener { _, _, _ ->
            chosenKey = null
            false
        }

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

                val key = chosenKey ?: toKey(label)
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
                binding.emptyState.visibility = View.GONE
                updateSummary()
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

        /** Representative data for the debug-only preview. Real values from a
         *  captured can and a peanut butter jar, including a missing field and
         *  an unexpected key, so the layout is exercised honestly. */
        val SAMPLE_JSON: String = """
            {"scanId":"SCAN-000069","status":"COMPLETED","overallConfidence":0.72,
             "persisted":true,"fields":{
              "batchNumber":{"value":"GSB0134","confidence":0.96,"band":"HIGH",
                "source":"OCR_RULES","displayName":"Batch number","expected":true},
              "manufacturingDate":{"value":"2024-11","confidence":0.88,"band":"REVIEW",
                "source":"OCR_RULES","displayName":"Manufacturing date","expected":true},
              "expiryDate":{"value":"2026-10","confidence":0.97,"band":"HIGH",
                "source":"OCR_RULES","displayName":"Expiry date","expected":true},
              "lotCode":{"value":null,"confidence":0.0,"band":"LOW",
                "source":"NOT_FOUND","displayName":"Lot code","expected":true},
              "mrp":{"value":"232.00","confidence":0.99,"band":"HIGH",
                "source":"OCR_RULES","displayName":"MRP","expected":true},
              "netContents":{"value":"510 g","confidence":0.9,"band":"REVIEW",
                "source":"OCR_RULES","displayName":"Net contents","expected":false}}}
        """.trimIndent()

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
