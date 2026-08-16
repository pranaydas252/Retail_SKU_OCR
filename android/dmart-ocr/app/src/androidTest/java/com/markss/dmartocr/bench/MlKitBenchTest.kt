package com.markss.dmartocr.bench

import android.graphics.BitmapFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Measures ML Kit on-device OCR against the same captures the server pipeline
 * is scored on.
 *
 * Why this exists: the server pipeline reaches 43% on the core fields with
 * PP-OCRv5, and a vision model raised that to 55% only by running two engines
 * and paying 8s per image. ML Kit is architecturally better suited than a
 * vision model to this input — it detects text regions and recognises each one
 * separately, so a small stamp inside a large pack shot costs it nothing,
 * whereas a VLM downscales the whole frame into a fixed token budget and
 * shrinks the very characters we need.
 *
 * Measured on the sample captures, printed text is a median 151px tall.
 * ML Kit asks for 16px per character and documents no benefit above 24px, so
 * these images carry roughly six times the resolution it can use. Whether it
 * copes with inkjet dot-matrix on curved metallic film is the open question,
 * and no published benchmark answers it for this packaging.
 *
 * This writes raw recognition output only. Scoring happens server-side with
 * the SAME extractor, normalizer and ground truth the other engines are judged
 * by, so the result is directly comparable rather than a fresh methodology:
 *
 *     ./gradlew connectedDebugAndroidTest
 *     adb pull /sdcard/Android/data/com.markss.dmartocr/files/mlkit_bench.json
 *     python scripts/score_mlkit.py mlkit_bench.json
 *
 * The captures are copied into the test APK's assets from the repository's
 * git-ignored images/ directory at build time, so there is nothing to push and
 * no ordering to get wrong.
 *
 * ML Kit is a debugImplementation dependency, so it does not reach the release
 * APK while its value is still unproven.
 */
@RunWith(AndroidJUnit4::class)
class MlKitBenchTest {

    @Test
    fun recognizeSampleCaptures() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val testContext = InstrumentationRegistry.getInstrumentation().context

        // No MlKit.initialize call here. ML Kit ships in the debug app (see
        // build.gradle.kts), so its ContentProvider has already run in this
        // process and initializing a second time throws.

        // Captures are bundled in the TEST apk's assets, so they are read from
        // the instrumentation context rather than the app under test.
        val assets = testContext.assets
        val names = assets.list("bench_in").orEmpty()
            .filter { it.substringAfterLast('.').lowercase() in setOf("jpg", "jpeg", "png") }
            .sorted()

        println("MLKIT_BENCH found ${names.size} captures in assets/bench_in")

        // Fail rather than skip. A skip reads as "nothing to do" and is easy to
        // miss in the runner output, but arriving here with no captures means
        // the run did not happen and the setup is wrong.
        check(names.isNotEmpty()) {
            "No captures in the test APK. They are copied from the repo's " +
                "images/ directory at build time; populate it and rebuild."
        }

        val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        val results = JSONArray()

        // The first call initialises the model, which costs far more than a
        // steady-state recognition. Timing that as if it were per-image would
        // misreport the latency by an order of magnitude, so it is warmed up on
        // the first capture and the reading discarded.
        warmUp(recognizer, decode(assets, names.first()))

        for (name in names) {
            val bitmap = decode(assets, name) ?: continue
            val input = InputImage.fromBitmap(bitmap, 0)

            val started = System.nanoTime()
            val text = com.google.android.gms.tasks.Tasks.await(
                recognizer.process(input), 60, TimeUnit.SECONDS
            )
            val elapsedMs = (System.nanoTime() - started) / 1_000_000.0

            // Lines, not blocks or elements. A line is the granularity the
            // server's extractor expects: on these labels a printed row holds a
            // field name and its value, which is exactly what the same-line
            // association rule is built to read. Blocks merge unrelated rows
            // and elements split a value from its label.
            val tokens = JSONArray()
            for (block in text.textBlocks) {
                for (line in block.lines) {
                    val box = line.boundingBox ?: continue
                    tokens.put(
                        JSONObject()
                            .put("text", line.text)
                            .put("x", box.left)
                            .put("y", box.top)
                            .put("width", box.width())
                            .put("height", box.height())
                    )
                }
            }

            results.put(
                JSONObject()
                    .put("image", name)
                    .put("width", bitmap.width)
                    .put("height", bitmap.height)
                    .put("ms", elapsedMs)
                    .put("tokens", tokens)
            )
            bitmap.recycle()
        }

        // Written to the app-under-test's external files dir, which adb CAN
        // read even though it cannot usefully write there.
        val output = File(context.getExternalFilesDir(null), "mlkit_bench.json")
        output.writeText(results.toString(2))
        println("MLKIT_BENCH wrote ${results.length()} results to ${output.absolutePath}")
    }

    private fun decode(assets: android.content.res.AssetManager, name: String) =
        assets.open("bench_in/$name").use { BitmapFactory.decodeStream(it) }

    private fun warmUp(
        recognizer: com.google.mlkit.vision.text.TextRecognizer,
        bitmap: android.graphics.Bitmap?,
    ) {
        if (bitmap == null) return
        runCatching {
            com.google.android.gms.tasks.Tasks.await(
                recognizer.process(InputImage.fromBitmap(bitmap, 0)), 60, TimeUnit.SECONDS
            )
        }
        bitmap.recycle()
    }
}
