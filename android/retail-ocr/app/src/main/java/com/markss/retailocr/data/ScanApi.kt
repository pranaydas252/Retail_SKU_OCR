package com.markss.retailocr.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import com.markss.retailocr.BuildConfig
import kotlinx.serialization.json.Json
import okhttp3.Interceptor
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import java.util.concurrent.TimeUnit

interface ScanApi {

    @GET("api/v1/health")
    suspend fun health(): HealthResponse

    @Multipart
    @POST("api/v1/scans")
    suspend fun createScan(
        @Part image: MultipartBody.Part,
        @Part("deviceId") deviceId: okhttp3.RequestBody,
        @Part("deviceModel") deviceModel: okhttp3.RequestBody,
    ): ScanResponse

    @POST("api/v1/scans/{scanId}/confirm")
    suspend fun confirmScan(
        @Path("scanId") scanId: String,
        @Body body: ConfirmRequest,
    ): ConfirmResponse

    /** Records that a label was printed. Called after the printer confirms. */
    @POST("api/v1/scans/{scanId}/print")
    suspend fun recordPrint(@Path("scanId") scanId: String): PrintResponse
}

object ApiClient {

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = true
    }

    /**
     * Timeouts sized against measured backend latency, not defaults.
     *
     * A 10-region label takes roughly 4s of CPU-only OCR, and a dense retail
     * pack will take longer because latency scales with the number of detected
     * text regions (PLAN.md R8). OkHttp's 10s default would cut off perfectly
     * healthy scans, so the read timeout is generous while connect stays short
     * so an unreachable server fails fast.
     */
    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(90, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .addInterceptor(baseUrlInterceptor())
            .addInterceptor(apiKeyInterceptor())
            .apply {
                if (BuildConfig.DEBUG) {
                    addInterceptor(
                        HttpLoggingInterceptor().apply {
                            // HEADERS, not BODY: response bodies carry full OCR
                            // token dumps and request bodies carry JPEGs.
                            level = HttpLoggingInterceptor.Level.HEADERS
                        }
                    )
                }
            }
            .build()
    }

    val api: ScanApi by lazy {
        Retrofit.Builder()
            // Placeholder. Retrofit fixes its base URL at build time, so the
            // real host is substituted per request by baseUrlInterceptor from
            // whatever the operator configured in settings.
            .baseUrl(BuildConfig.BACKEND_BASE_URL)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(ScanApi::class.java)
    }

    /**
     * Rewrites each request onto the currently configured server.
     *
     * Retrofit cannot change its base URL after construction, and rebuilding
     * the client whenever settings change would leak connection pools and race
     * with in-flight calls. Swapping scheme, host and port per request keeps
     * one client and makes a settings change take effect immediately.
     */
    private fun baseUrlInterceptor() = Interceptor { chain ->
        val configured = AppPreferences.serverUrl.toHttpUrlOrNull()
            ?: return@Interceptor chain.proceed(chain.request())

        val original = chain.request()
        val rewritten = original.url.newBuilder()
            .scheme(configured.scheme)
            .host(configured.host)
            .port(configured.port)
            .build()

        chain.proceed(original.newBuilder().url(rewritten).build())
    }

    /** Attaches the API key when one is configured (CLAUDE.md section 19). */
    private fun apiKeyInterceptor() = Interceptor { chain ->
        val key = BuildConfig.API_KEY
        val request = if (key.isNotEmpty()) {
            chain.request().newBuilder().addHeader("X-API-Key", key).build()
        } else {
            chain.request()
        }
        chain.proceed(request)
    }

    fun textPart(value: String): okhttp3.RequestBody =
        value.toRequestBody("text/plain".toMediaType())

    fun imagePart(bytes: ByteArray, filename: String = "label.jpg"): MultipartBody.Part =
        MultipartBody.Part.createFormData(
            "image",
            filename,
            bytes.toRequestBody("image/jpeg".toMediaType()),
        )
}
