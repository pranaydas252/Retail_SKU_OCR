package com.markss.dmartocr.print

import android.util.Log
import com.zebra.sdk.comm.Connection
import com.zebra.sdk.comm.ConnectionException

/**
 * Opening the Bluetooth link to the printer, with one retry.
 *
 * Android's RFCOMM stack fails the first `connect()` to a bonded device often
 * enough that it has to be designed for, not treated as an error. Observed on
 * the TC22 against a ZQ320 that was switched on, bonded and inches away:
 *
 *     ConnectionException: Could not connect to device:
 *     read failed, socket might closed or timeout, read ret: -1
 *
 * The same print six seconds later succeeded with no intervention. Without a
 * retry the operator is shown "check it is on, paired and in range" about a
 * printer that is all three, and the only way through is to press print again.
 *
 * **The retry covers the connection attempt only.** By the time bytes have been
 * written, a failure could mean a partial write, and reconnecting to send them
 * again risks a second label. A duplicate label is worse than a failed one: it
 * wastes media and puts two different labels on one product.
 *
 * Kept free of Context and of the printer class so it can be tested on the JVM
 * with a fake factory. A retry loop that nothing exercises is exactly the kind
 * that turns into an infinite one.
 */
internal object PrinterConnect {

    /**
     * Two attempts, not more.
     *
     * One retry is enough for the transient case, and every extra attempt is
     * paid for by the operator whose printer is genuinely switched off — a
     * failing `open()` blocks for seconds before it throws, so three attempts
     * would mean waiting three times as long to be told the obvious.
     */
    const val ATTEMPTS = 2

    /** Long enough for the stack to settle, short enough to go unnoticed. */
    const val RETRY_DELAY_MS = 500L

    private const val TAG = "BtLabelPrinter"

    /**
     * Opens a connection, retrying a failed attempt once.
     *
     * @param factory builds a fresh connection per attempt. Reusing a
     *   connection whose open() threw does not work — the underlying socket is
     *   already spent.
     * @throws ConnectionException from the final attempt, so the caller sees
     *   the real reason rather than a synthesized one.
     */
    fun open(
        mac: String,
        attempts: Int = ATTEMPTS,
        delayMs: Long = RETRY_DELAY_MS,
        sleep: (Long) -> Unit = { Thread.sleep(it) },
        log: (String) -> Unit = { Log.w(TAG, it) },
        factory: (String) -> Connection,
    ): Connection {
        require(attempts >= 1) { "attempts must be at least 1" }

        var attempt = 1
        while (true) {
            val connection = factory(mac)
            try {
                connection.open()
                if (attempt > 1) log("Connected to $mac on attempt $attempt")
                return connection
            } catch (e: ConnectionException) {
                // Close the spent socket before trying again, or the retry
                // contends with a half-open connection to the same device.
                runCatching { connection.close() }

                if (attempt >= attempts) throw e

                log("Connect attempt $attempt to $mac failed (${e.message}); retrying")
                attempt++
                sleep(delayMs)
            }
        }
    }
}
