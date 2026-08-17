package com.markss.retailocr.data

import android.content.Context
import android.os.Environment
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Writes ROI crops to device storage for offline collection.
 *
 * Uses the app's external files directory, which needs no runtime permission
 * and survives `adb pull`. Files land at:
 *
 *     /sdcard/Android/data/com.markss.retailocr/files/Pictures/samples/
 *
 * The point is to gather what the app actually sends — a tight ROI crop —
 * rather than the full-frame photographs the accuracy harness currently scores,
 * which are a harder input than operators will ever produce.
 */
object SampleStore {

    private const val TAG = "SampleStore"
    private const val FOLDER = "samples"

    fun directory(context: Context): File {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_PICTURES)
        return File(base, FOLDER).apply { mkdirs() }
    }

    /** Saves one crop. Returns the running total, or null if the write failed. */
    fun save(context: Context, jpeg: ByteArray): Int? = try {
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
        val target = File(directory(context), "SAMPLE_$stamp.jpg")
        target.writeBytes(jpeg)
        val total = count(context)
        Log.d(TAG, "Saved ${target.name} (${jpeg.size} bytes), $total total")
        total
    } catch (e: Exception) {
        Log.e(TAG, "Could not save sample", e)
        null
    }

    fun count(context: Context): Int =
        directory(context).listFiles { f -> f.extension.equals("jpg", true) }?.size ?: 0
}
