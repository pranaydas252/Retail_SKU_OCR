package com.markss.dmartocr.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.markss.dmartocr.R
import com.markss.dmartocr.data.ApiClient
import com.markss.dmartocr.data.ConfirmRequest
import com.markss.dmartocr.data.ExtractedField
import com.markss.dmartocr.data.ScanResponse
import com.markss.dmartocr.databinding.ActivityResultBinding
import com.markss.dmartocr.databinding.ItemFieldBinding
import com.markss.dmartocr.device.DeviceId
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

        binding.retakeButton.setOnClickListener { finish() }
        binding.confirmButton.setOnClickListener { confirm() }
    }

    private fun buildFields() {
        val inflater = LayoutInflater.from(this)

        // Worst first. The values most likely to be wrong must not sit below
        // the fold where a hurried operator will scroll past them.
        val ordered = FIELD_ORDER
            .filter { scan.fields.containsKey(it) }
            .sortedBy { name ->
                val field = scan.fields[name]!!
                when {
                    !field.wasFound -> 0
                    field.band == ExtractedField.BAND_LOW -> 1
                    field.band == ExtractedField.BAND_REVIEW -> 2
                    else -> 3
                }
            }

        ordered.forEach { name ->
            val field = scan.fields[name]!!
            val row = ItemFieldBinding.inflate(inflater, binding.fieldContainer, false)

            row.fieldLabel.setText(labelFor(name))
            row.fieldValue.setText(field.value ?: "")
            applyBand(row, field)

            // Editing invalidates the band: the server's confidence described
            // the value it read, not the one the operator just typed. Leaving a
            // green "clear" chip on an edited field would assert something the
            // system no longer knows.
            row.fieldValue.setOnFocusChangeListener { _, hasFocus ->
                if (!hasFocus) markEdited(row, field)
            }

            rows[name] = row
            binding.fieldContainer.addView(row.root)
        }
    }

    private fun applyBand(row: ItemFieldBinding, field: ExtractedField) {
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
        val needingReview = scan.fields.count { (_, field) ->
            !field.wasFound || field.band != ExtractedField.BAND_HIGH
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
                onSuccess = {
                    setBusy(false)
                    finish()
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

    private fun setBusy(busy: Boolean) {
        binding.confirmButton.isEnabled = !busy
        binding.retakeButton.isEnabled = !busy
        binding.confirmButton.setText(
            if (busy) R.string.result_saving else R.string.result_confirm
        )
    }

    private fun labelFor(field: String): Int = when (field) {
        "batchNumber" -> R.string.field_batch_number
        "manufacturingDate" -> R.string.field_manufacturing_date
        "expiryDate" -> R.string.field_expiry_date
        "lotCode" -> R.string.field_lot_code
        "mrp" -> R.string.field_mrp
        else -> R.string.field_batch_number
    }

    companion object {
        const val EXTRA_SCAN_JSON = "scan_json"

        private val json = Json {
            ignoreUnknownKeys = true
            explicitNulls = false
            encodeDefaults = true
        }

        /** Display order before confidence sorting is applied. */
        private val FIELD_ORDER = listOf(
            "batchNumber",
            "manufacturingDate",
            "expiryDate",
            "lotCode",
            "mrp",
        )

        // Serializers passed explicitly: inside a companion object the bare
        // call resolves to the member overload that expects a
        // SerializationStrategy rather than the reified extension.
        fun encode(response: ScanResponse): String =
            json.encodeToString(ScanResponse.serializer(), response)

        fun decode(payload: String): ScanResponse =
            json.decodeFromString(ScanResponse.serializer(), payload)
    }
}
