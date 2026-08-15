package com.markss.dmartocr.print

import android.Manifest
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat
import com.markss.dmartocr.data.AppPreferences
import com.zebra.sdk.comm.BluetoothConnection
import com.zebra.sdk.comm.Connection
import com.zebra.sdk.comm.ConnectionException
import com.zebra.sdk.printer.PrinterStatus
import com.zebra.sdk.printer.ZebraPrinterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Locale

/**
 * Prints the confirmed-scan label on a Zebra ZQ320 over Bluetooth, using the
 * Link-OS Multiplatform SDK (CLAUDE.md section 18).
 *
 * The SDK jar is vendored into `app/libs` and git-ignored: it is not published
 * to Maven Central and is only distributed as a manual download from Zebra
 * behind an EULA. The dependency declaration is committed, the binary is not.
 *
 * Using the SDK rather than a raw RFCOMM socket buys the one thing that
 * matters here: [PrinterStatus]. A socket write to a printer that is out of
 * paper or has its head open succeeds at the transport level and silently
 * produces nothing, which is exactly the silent failure section 18 forbids.
 * Checking status first turns that into a message the operator can act on.
 */
class BluetoothLabelPrinter(private val context: Context) : LabelPrinter {

    /**
     * Neither check here needs BLUETOOTH_CONNECT: reading preferences is local,
     * and [android.bluetooth.BluetoothAdapter.isEnabled] is not permission
     * guarded. That is what makes it safe to run before prompting.
     */
    override fun preflight(): PrintResult.Failure? {
        if (AppPreferences.printerMac.isBlank()) {
            return PrintResult.Failure(
                PrintResult.Reason.NOT_CONFIGURED,
                "No printer configured. Set the ZQ320 Bluetooth address in settings.",
                retryable = false,
            )
        }

        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE)
            as? BluetoothManager)?.adapter
        if (adapter == null) {
            return PrintResult.Failure(
                PrintResult.Reason.BLUETOOTH_OFF,
                "This device has no Bluetooth adapter.",
                retryable = false,
            )
        }
        if (!adapter.isEnabled) {
            return PrintResult.Failure(
                PrintResult.Reason.BLUETOOTH_OFF,
                "Bluetooth is switched off. Turn it on and try again.",
            )
        }

        return null
    }

    override suspend fun print(scan: ConfirmedScan): PrintResult =
        withContext(Dispatchers.IO) {
            preflight()?.let { return@withContext it }

            val mac = AppPreferences.printerMac.trim().uppercase(Locale.US)

            val missing = missingPermissions()
            if (missing.isNotEmpty()) {
                return@withContext PrintResult.Failure(
                    PrintResult.Reason.PERMISSION_DENIED,
                    "Bluetooth permission is required to print.",
                    retryable = true,
                )
            }

            val zpl = try {
                buildZpl(scan)
            } catch (e: IllegalArgumentException) {
                Log.e(TAG, "Payload rejected", e)
                return@withContext PrintResult.Failure(
                    PrintResult.Reason.PAYLOAD_TOO_LARGE,
                    e.message ?: "The QR payload is too large for a 10mm symbol.",
                    retryable = false,
                )
            }

            send(mac, zpl)
        }

    private fun buildZpl(scan: ConfirmedScan): String = ZplBuilder.label(
        scanCode = scan.scanCode,
        rows = ROW_ORDER.mapNotNull { (key, label) ->
            scan.fields[key]?.takeIf { it.isNotBlank() }?.let { ZplBuilder.Row(label, it) }
        },
        qrPayload = scan.qrPayload,
    )

    private fun send(mac: String, zpl: String): PrintResult {
        var connection: Connection? = null
        return try {
            connection = BluetoothConnection(mac)
            connection.open()

            val status = ZebraPrinterFactory.getInstance(connection).currentStatus
            describeBlockingStatus(status)?.let { message ->
                Log.w(TAG, "Printer not ready: $message")
                return PrintResult.Failure(PrintResult.Reason.NOT_READY, message)
            }

            connection.write(zpl.toByteArray(Charsets.UTF_8))

            // Let the printer drain before the connection drops. Closing
            // straight after write truncates the job on a slow link — the bytes
            // have left our buffer but not the radio.
            Thread.sleep(DRAIN_PAUSE_MS)

            Log.d(TAG, "Sent ${zpl.length} bytes to $mac")
            PrintResult.Success
        } catch (e: ConnectionException) {
            Log.e(TAG, "Print failed to $mac", e)
            PrintResult.Failure(
                PrintResult.Reason.CONNECT_FAILED,
                "Could not reach the printer. Check it is on, paired and in range.",
            )
        } catch (e: SecurityException) {
            // Reaching here means a permission was refused mid-call rather than
            // being absent up front. Report what the system actually said
            // instead of asserting a cause — the previous version blamed
            // BLUETOOTH_CONNECT for a BLUETOOTH_SCAN failure, which sent
            // everyone looking in the wrong place.
            Log.e(TAG, "Bluetooth call rejected", e)
            PrintResult.Failure(
                PrintResult.Reason.PERMISSION_DENIED,
                e.message ?: "Bluetooth access was refused.",
                retryable = true,
            )
        } catch (e: Exception) {
            Log.e(TAG, "Unexpected print failure", e)
            PrintResult.Failure(
                PrintResult.Reason.WRITE_FAILED,
                e.message ?: "The label could not be printed.",
            )
        } finally {
            runCatching { connection?.close() }
        }
    }

    /**
     * Returns an operator-facing message when the printer cannot print, or null
     * when it is ready.
     *
     * Paused is treated as blocking: a paused ZQ320 accepts the job and holds
     * it, so the operator would walk away believing a label was produced.
     *
     * "Out of labels" on a printer that visibly has media almost always means
     * media SENSING, not media absence: the printer is calibrated for gap or
     * black-mark stock and is being fed continuous stock (or the reverse), so
     * it feeds looking for a mark it cannot find and reports itself empty. The
     * message says so, because "out of labels" sends the operator to reload
     * paper that is already there.
     */
    private fun describeBlockingStatus(status: PrinterStatus): String? = when {
        status.isReadyToPrint -> null
        status.isHeadOpen -> "The printer cover is open. Close it and try again."
        status.isPaused -> "The printer is paused. Take it off pause and try again."
        status.isPaperOut ->
            "The printer reports no media. If labels ARE loaded, it is calibrated " +
                "for a different media type — run a calibration from the printer, " +
                "or switch media mode in settings."
        else -> "The printer is not ready (${describe(status)})."
    }

    /** Raw status flags, for a log line that can actually be diagnosed. */
    private fun describe(status: PrinterStatus): String = buildString {
        append("ready=").append(status.isReadyToPrint)
        append(" paperOut=").append(status.isPaperOut)
        append(" headOpen=").append(status.isHeadOpen)
        append(" paused=").append(status.isPaused)
        append(" partialFormat=").append(status.isPartialFormatInProgress)
        append(" buffersFull=").append(status.isReceiveBufferFull)
    }

    /**
     * Prints a small self-test label and reports the printer's own status.
     *
     * Exists so the printer can be proven independently of a scan. Diagnosing a
     * print failure by repeatedly photographing a product label is slow and
     * confounds two subsystems.
     */
    suspend fun testPrint(): PrintResult = withContext(Dispatchers.IO) {
        preflight()?.let { return@withContext it }
        if (missingPermissions().isNotEmpty()) {
            return@withContext PrintResult.Failure(
                PrintResult.Reason.PERMISSION_DENIED,
                "Bluetooth permission is required to print.",
            )
        }
        send(
            AppPreferences.printerMac.trim().uppercase(Locale.US),
            ZplBuilder.testLabel(),
        )
    }

    /**
     * Runtime Bluetooth permissions that are still missing.
     *
     * Both are needed. BLUETOOTH_CONNECT is obvious. BLUETOOTH_SCAN is not:
     * this app never discovers devices, but the Link-OS SDK calls
     * cancelDiscovery() internally while opening a connection and that call is
     * scan-guarded on API 31+. Omitting it produces a SecurityException naming
     * a permission the operator cannot connect to anything they did.
     *
     * Below API 31 the install-time BLUETOOTH permission covers both, so there
     * is nothing to ask for.
     */
    fun missingPermissions(): List<String> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return emptyList()
        return REQUIRED_PERMISSIONS.filter {
            ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
        }
    }

    companion object {
        private const val TAG = "BtLabelPrinter"

        /** Runtime permissions needed on API 31+. See [missingPermissions]. */
        @RequiresApi(Build.VERSION_CODES.S)
        val REQUIRED_PERMISSIONS = listOf(
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.BLUETOOTH_SCAN,
        )
        private const val DRAIN_PAUSE_MS = 250L

        /** Printed rows, in label order (CLAUDE.md section 18). */
        private val ROW_ORDER = listOf(
            "batchNumber" to "BATCH",
            "manufacturingDate" to "MFG",
            "expiryDate" to "EXP",
            "lotCode" to "LOT",
            "mrp" to "MRP",
        )
    }
}
