package com.markss.retailocr.ui

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
import com.markss.retailocr.R
import com.markss.retailocr.data.ApiClient
import com.markss.retailocr.data.ConfirmRequest
import com.markss.retailocr.data.ExtractedField
import com.markss.retailocr.data.ScanResponse
import com.markss.retailocr.databinding.ActivityResultBinding
import com.markss.retailocr.databinding.DialogAddFieldBinding
import com.markss.retailocr.databinding.ItemFieldBinding
import com.markss.retailocr.device.DeviceId
import com.markss.retailocr.print.BluetoothLabelPrinter
import com.markss.retailocr.print.ConfirmedScan
import com.markss.retailocr.print.LabelPrinter
import com.markss.retailocr.print.PrintResult
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

        // The SKU is not an OCR result. It arrives decoded from the pack's
        // barcode, so it joins the field set here rather than coming back from
        // the server, and it is the one value on this screen the operator has
        // no reason to check. Injecting it as an ordinary field means it edits,
        // confirms, persists and prints through the paths that already exist.
        //
        // Absent when the operator skipped the barcode step, in which case no
        // card appears at all — the same rule as every other field.
        intent.getStringExtra(EXTRA_SKU_CODE)?.takeIf { it.isNotBlank() }?.let { sku ->
            scan = scan.copy(
                fields = scan.fields + (FIELD_SKU_CODE to ExtractedField(
                    value = sku,
                    confidence = 1.0,
                    band = ExtractedField.BAND_HIGH,
                    source = ExtractedField.SOURCE_BARCODE,
                    displayName = getString(R.string.field_sku),
                    expected = false,
                ))
            )
        }

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

        offerBothReadings(row, field)

        rows[name] = row
        // A contested field takes the full width. Two dates do not fit side by
        // side in a half-width cell, and the field the engines disagree about
        // is the one most worth the operator's attention anyway.
        binding.fieldContainer.addView(row.root, cellParams(wide = field.isContested))
    }

    /**
     * Shows both engines' readings when they disagree, and lets the operator pick.
     *
     * Neither engine is reliably right. Measured on the 20-image corpus,
     * PP-OCRv5 wins on printed text and the vision-language model on inkjet
     * stamps, and the merge resolves a contest by simply keeping the primary —
     * not because it is more often correct, but so the result stays
     * predictable. That makes the displayed value a coin flip presented as a
     * fact unless the alternative is shown too.
     *
     * The operator is holding the pack. They can settle in a second what no
     * tie-break rule can settle at all.
     */
    private fun offerBothReadings(row: ItemFieldBinding, field: ExtractedField) {
        val other = field.conflictValue
        if (other == null || field.value == null) {
            row.choiceRow.visibility = View.GONE
            return
        }

        row.choiceRow.visibility = View.VISIBLE
        row.choicePrimaryValue.text = field.value
        row.choiceSecondaryValue.text = other

        fun select(chosen: String) {
            row.fieldValue.setText(chosen)
            val primaryChosen = chosen == field.value
            styleChip(row, primary = true, selected = primaryChosen)
            styleChip(row, primary = false, selected = !primaryChosen)
        }

        // Nothing is preselected. Highlighting the primary would restore
        // exactly the false certainty this row exists to remove, and the
        // EditText already holds it, so the operator can also just confirm.
        styleChip(row, primary = true, selected = false)
        styleChip(row, primary = false, selected = false)

        row.choicePrimary.setOnClickListener { select(field.value) }
        row.choiceSecondary.setOnClickListener { select(other) }
    }

    /**
     * Brand accent for the reading the operator picked; sunken for the other.
     *
     * The engine name stays quieter than the value at both states. It is there
     * to say where a reading came from, not to compete with the reading itself.
     */
    private fun styleChip(row: ItemFieldBinding, primary: Boolean, selected: Boolean) {
        val chip = if (primary) row.choicePrimary else row.choiceSecondary
        val label = if (primary) row.choicePrimaryLabel else row.choiceSecondaryLabel
        val value = if (primary) row.choicePrimaryValue else row.choiceSecondaryValue

        chip.backgroundTintList = ContextCompat.getColorStateList(
            this,
            if (selected) R.color.brand_cyan_container else R.color.surface_sunken,
        )
        value.setTextColor(
            ContextCompat.getColor(
                this,
                if (selected) R.color.brand_cyan_pressed else R.color.text_primary,
            )
        )
        label.setTextColor(
            ContextCompat.getColor(
                this,
                if (selected) R.color.brand_cyan_pressed else R.color.text_tertiary,
            )
        )
    }

    /**
     * Half-width cell in the two-column grid.
     *
     * GridLayout children default to WRAP_CONTENT and no weight, which makes
     * every card size to its own text and leaves a ragged right edge. A column
     * weight with a zero base width is what makes the two columns equal.
     */
    private fun cellParams(wide: Boolean = false): GridLayout.LayoutParams =
        GridLayout.LayoutParams().apply {
            width = 0
            height = ViewGroup.LayoutParams.WRAP_CONTENT
            columnSpec =
                if (wide) GridLayout.spec(GridLayout.UNDEFINED, 2, 1f)
                else GridLayout.spec(GridLayout.UNDEFINED, 1f)
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

            // Ahead of the band, because it says something the band cannot.
            // "Check against pack" tells the operator to be careful; this tells
            // them exactly what the disagreement is and offers both answers.
            field.isContested -> Triple(
                R.drawable.dot_review, R.string.result_contested, R.color.band_review
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
     * It counts only values that are on screen and doubtful. A field the
     * server did not find is not mentioned: it has no card, there is nothing
     * to review, and naming it only asks the operator to account for something
     * the pack may simply not print. "Add field" is there for the case where
     * it does.
     */
    private fun updateSummary() {
        val needingReview = scan.fields.count { (name, field) ->
            rows.containsKey(name) && field.band != ExtractedField.BAND_HIGH
        }

        val summary: String? = when {
            needingReview == 1 -> getString(R.string.result_review_count, 1)
            needingReview > 1 ->
                getString(R.string.result_review_count_plural, needingReview)

            else -> null
        }

        // Nothing was read at all, so there is nothing to be clear about. The
        // banner used to say "All values read clearly" above a card explaining
        // that no fields could be read, because zero doubtful values and zero
        // values look identical to a count.
        if (rows.isEmpty()) {
            binding.summaryBanner.visibility = View.GONE
            return
        }
        binding.summaryBanner.visibility = View.VISIBLE

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
        const val EXTRA_SKU_CODE = "sku_code"

        /** Must match the backend field name in config/field_aliases.yaml. */
        const val FIELD_SKU_CODE = "skuCode"

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
