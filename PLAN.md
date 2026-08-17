# Reliance Retail SKU Label OCR — Phase Plan

Scope authority is [CLAUDE.md](CLAUDE.md). This file is the execution plan only — where CLAUDE.md and this file disagree, CLAUDE.md wins.

Section references below (§n) point at CLAUDE.md sections.

---

## 0. Environment Audit (verified 2026-08-14)

| Component | Status | Notes |
| --- | --- | --- |
| Python | 3.12.10 | OK |
| FastAPI / Uvicorn | 0.111.0 / 0.29.0 | Installed |
| Pydantic / pydantic-settings | 2.7.1 / 2.2.1 | Installed |
| OpenCV | opencv-contrib-python 4.10.0.84 | Installed |
| PaddleOCR / PaddlePaddle | 3.4.1 / 3.3.1 (CPU) | Installed — **3.x API, not 2.x** |
| pyodbc | 5.1.0 | Installed |
| SQL Server | `MSSQL$SQLEXPRESS` running | Local instance available |
| ODBC Driver | 18 and 17 for SQL Server | Use Driver 18 |
| sqlcmd | 16.0.1000.6 | Available |
| JDK | 17.0.5 LTS | OK for AGP 8.x |
| Android SDK | platforms to android-37, build-tools to 36.1.0 | At `%LOCALAPPDATA%\Android\Sdk` |
| Android Studio | Installed | — |
| Gradle CLI | **Not on PATH** | Use Gradle wrapper (`gradlew`) |
| `ANDROID_HOME` / `JAVA_HOME` | **Unset** | Needed only for CLI/CI builds |
| Connected device | Samsung SM-S711B, Android 16 (SDK 36) | **Not the TC22** — dev device only |
| Git | 2.52.0 | Repo **not yet initialized** |

**Consequence:** Phases 1–3 (backend) can start immediately with zero procurement. Phases 4–6 have hardware/config dependencies listed in §Open Decisions.

---

## 1. Risk Register (address before the phase that hits them)

| # | Risk | Impact | Mitigation | Phase | Status |
| --- | --- | --- | --- | --- | --- |
| R1 | PaddleOCR **3.x** API differs from the 2.x style implied by common examples | Silent breakage / rewrite | **Confirmed by introspection** — see §1a below. Paddle imports confined to `ocr_service.py`; everything downstream eats our own `OcrToken(text, bbox, confidence)` | 1 | Characterized |
| R2 | First PaddleOCR run downloads models from the network | Slow first scan / air-gapped failure | **Accepted by user.** Models pre-initialized before demo. `scripts/warmup_models.py` + startup warmup inference + Android "preparing OCR" state. Model dir configurable, vendored to `models/` for deploy | 1 / 6 | Resolved |
| R3 | No real Reliance Retail label images yet, and **public datasets are dropped** — PaddleOCR is used zero-shot | No accuracy gate can run until images arrive | **Accepted.** Extraction logic is built and unit-tested against hand-written token fixtures; the accuracy number arrives with the images. `sample_data/` structure stays ready | 2 | Accepted |
| R4 | Indian retail labels often carry Devanagari/regional script alongside English | Junk tokens, bad spatial association | Start `lang="en"`; evaluate multilingual only if real images demand it. Log unrecognized-script rate | 2 | Open |
| R5 | TC22 not yet connected | Camera/printer assumptions untested | Develop on SM-S711B; TC22 arriving. See R10 — the Zebra-only gate must not block this | 4 | Open |
| R10 | **Zebra-only device gate blocks all development** until a TC22 physically arrives | Phase 4 stalls entirely | Gate driven by a build-config flag: `debug → ENFORCE_ZEBRA_ONLY=false`, `release → true`. Debug bypass logs a loud warning and is never enabled in release | 4 | Mitigated by design |
| R11 | EMDK `PackageManager` check silently fails on **every** device, Zebra included, if the Android 11+ `<queries>` element is missing | Gate rejects genuine TC22s; looks like a hardware fault | Manifest must declare `<queries><package android:name="com.symbol.emdk.emdkservice" /></queries>`. Add a test that asserts its presence | 4 | Known |
| R12 | Client device check is spoofable; APK installs on non-Zebra hardware | Restriction is advisory, not enforced | **Accepted by user, no server-side allowlist.** The app is useless off a TC22 because the scan flow refuses to start, and distribution is a controlled store estate. Do not add server-side device validation unless asked | 4 | Accepted |
| R6 | ZQ320 needs Zebra ZSDK (Link-OS AAR, not on Maven Central) | Build blocked | Obtain SDK during Phase 3 so it is in hand before Phase 5. Vendor to `android/retail-ocr/app/libs/` (git-ignored) | 5 | Open |
| R7 | Self-signed HTTPS cert rejected by TC22 | End-to-end blocked at the last step | Decide cert strategy before Phase 6; if self-signed, plan Android network-security-config + device trust store install | 6 | Open |
| R8 | CPU-only OCR latency **scales with detected region count, not image size** — measured 1.6 s for a 2-region image but **3.9–4.1 s for a 10-region label**. Real SKU labels carry far more text (ingredients, addresses, barcodes, statutory text) and could plausibly hit 30–60 regions | Poor operator UX; possibly unusable | Partly addressed (see §1c). **Dominant remaining lever: the fixed ROI window (§4).** The app crops the capture to the on-screen ROI automatically — no operator crop step — so OCR never sees surrounding packaging. Phase 4 change. Only if p95 is still bad, add the optional WebSocket (§14) — **no queue** | 1 → 4 → 7 | Measured, mitigation designed |
| R14 | ROI crop uses preview coordinates against a full-resolution capture | Wrong region cropped, subtly rather than obviously — bad OCR that looks like a model problem | Bind preview + capture to a shared `ViewPort` in a `UseCaseGroup` so CameraX derives a consistent crop rect. Verify the mapping on the TC22, not on the dev device | 4 | Known |
| R9 | QR must fit 10 mm × 10 mm **and** carry all confirmed fields | Unscannable label | Budget computed — see §1b. Fits with headroom. Enforce a payload-length check before emitting ZPL | 5 | Resolved |

### 1a. PaddleOCR 3.4.1 API — verified by introspection

Confirmed on the dev machine, not assumed:

- `use_angle_cls` **does not exist**. It is `use_textline_orientation`.
- `predict()` is the current inference entry point.
- Models are chosen by name: `text_detection_model_name`, `text_recognition_model_name`.
- Pipeline version selectable via `ocr_version`.
- Thresholds are constructor args: `text_det_limit_side_len`, `text_det_limit_type`, `text_det_thresh`, `text_det_box_thresh`, `text_rec_score_thresh`.
- The pipeline ships `use_doc_orientation_classify` and a **document unwarping** model — these overlap with the OpenCV rotation and perspective work in §7, and unwarping is directly relevant to the `curved_packaging/` bucket. Evaluate before hand-rolling equivalents.

**Version decision: keep `paddleocr==3.4.1` / `paddlepaddle==3.3.1`.** The 3.x line is the line that ships PP-OCRv5. Downgrading to 2.x would lose PP-OCRv5 entirely.

**Model decision: `PP-OCRv5_mobile_det` + `PP-OCRv5_mobile_rec`.**

Originally planned as `server_det + mobile_rec` from published benchmarks. **Measured on this machine, that was wrong:**

| Detection model | Median warm latency | Text output | Scores |
| --- | --- | --- | --- |
| `PP-OCRv5_mobile_det` | **1.60 s** | correct | 0.954 / 0.962 |
| `PP-OCRv5_server_det` | 12.23 s | identical | 0.934 / 0.990 |

Published figures implied a ~2.5× gap; the real gap here is **7.6×**, and server detection bought nothing on this input. 10.6 extra seconds per scan is not a defensible trade for an operator workflow.

Caveat, stated honestly: the test image was clean synthetic text — the easy case. Server detection's advantage should appear on glare, blur, and small dense print, which is exactly what cannot be measured until real Reliance Retail labels exist. Both model names stay config-driven, and this decision is re-opened at the Phase 2 accuracy gate.

**Runtime blocker found and fixed.** PaddlePaddle 3.3.1 on Windows CPU crashes during text detection:

```text
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
not support [pir::ArrayAttribute<pir::DoubleAttribute>]
(at ..\paddle\fluid\framework\new_executor\instruction\onednn\onednn_instruction.cc:118)
```

This is a bug in Paddle's oneDNN backend, not in our code. Workaround: `enable_mkldnn=False`, exposed as `OCR_ENABLE_MKLDNN`. Detection is entirely non-functional without it — this would have blocked Phase 1 completely.

**Also noted:** PaddleOCR ignores `lang` and `ocr_version` when explicit model names are given, and warns. We pass model names only, so neither is sent — passing them would imply a control that does not exist.

### 1c. Latency findings (Phase 1, measured)

Reference image: 900 × 640 synthetic label, 10 detected text regions, `mobile_det` + `mobile_rec`, oneDNN off.

| Change | Median `ocrMs` |
| --- | --- |
| Starting point | 5.0 – 6.9 s |
| `text_recognition_batch_size` 6 → **1** | 6.05 s → **3.94 s** standalone |
| Scan endpoint `async def` → **`def`** | → **3.9 – 4.1 s** through the server |

Two findings behind those numbers:

**Recognition batch size 1 is fastest on CPU**, against intuition. Batched recognition pads every crop to the widest in the batch, and label text varies wildly in width, so larger batches mostly process padding. Verified with construction order reversed to rule out cache-ordering effects: batch 1 = 3.94 s, batch 6 = 6.05 s, batch 16 = 6.43 s.

**The scan endpoint was declared `async def` while calling blocking OCR**, which pinned several seconds of CPU work to the event loop. That both inflated latency by 1–3 s and made the whole server unresponsive during a scan. Declaring it `def` lets FastAPI run it in a threadpool. After the fix, `/health` answers in **49 ms** while a scan is in flight.

**Latency scales with region count, not image size.** 2 regions ≈ 1.6 s, 10 regions ≈ 4.0 s. This is the number that matters for R8, and it is why cropping to the target rectangle on-device is the main remaining lever.

### 1b. QR density budget — 10 mm × 10 mm

ZQ320 head is 203 dpi = 8 dots/mm, so 10 mm = **80 dots**.

| Setting | Value |
| --- | --- |
| ZPL `^BQ` magnification | 2 → module = 2 dots = 0.25 mm |
| Modules in 80 dots | 40 |
| QR version | 5 (37 × 37 = 74 dots = 9.25 mm) |
| Error correction | M |
| Capacity | ~154 alphanumeric chars / ~106 bytes |

Payload `SCAN-000001|A23C91|2026-07|2028-06|K18P2|245.00` = **47 chars**. Comfortable headroom for extra fields.

Constraints: magnification must not drop below 2 (1-dot modules are unreliable on thermal media); the 4-module quiet zone sits **outside** the 10 mm symbol and must be reserved in the template; prefer uppercase alphanumeric, since mixed case forces byte mode and roughly halves capacity.

---

## Phase 0 — Repo Skeleton & Configuration Contract  ✅ DONE

**Goal:** a committable repo whose shape matches §29, with all config externalized before any code depends on it.

**Delivered**
- `git init`, initial commit, remote `https://github.com/pranaydas252/Retail_SKU_OCR.git`
- `.gitignore` — secrets, Python caches, Paddle model binaries + `.paddlex`/`.paddleocr` caches, runtime `images/`/`logs/`/`debug/`, sample-data image binaries (ground-truth JSON kept), Gradle/Android build output, keystores, vendored ZSDK AAR, IDE and OS noise
- Folder layout per revised §29 — backend at root, `android/` for the Gradle project
- `requirements.txt` pinned to the audited versions
- `.env.example` — API key, SQL connection string, image root, OCR model names + thresholds + warmup, field alias config, confidence bands, printer/QR geometry (§19)
- `README.md` — stack, setup, verified environment
- `sample_data/README.md` — bucket definitions, ground-truth format, ranked public dataset list

**Remaining for Phase 1:** `app/config.py` (`pydantic-settings` `Settings`) is the single source of config truth — no literal paths, URLs, or credentials anywhere else (§28).

**Exit gate:** repo pushed, structure matches spec. Met.

**Est.** 0.5 day — complete

---

## Phase 1 — Backend OCR Spine (§26 P1 + P2)  ✅ DONE

**Goal:** image in → OCR tokens with bounding boxes out. This is the load-bearing phase; the rest is logic on top of it.

**Verified end to end.** `POST /api/v1/scans` on a synthetic label returns 10 tokens with bounding boxes, all correctly read at 0.95–0.999 confidence, in ~4.0 s. Labels and values come back as *separate* tokens — which is exactly what Phase 2 spatial association needs. Error paths confirmed: unreadable image → 400 `UNREADABLE_IMAGE`, undersized → 400 `IMAGE_TOO_SMALL`, blank image → 200 `NO_TEXT_DETECTED` with a recapture message. 37 tests pass.

Database schema is also applied ahead of Phase 3: `SkuScan`, `SkuScanField`, `SkuScanOcr`, `SkuMaster`, and the `SeqScanCode` sequence exist in `RetailOcr` on `MSSQL$SQLEXPRESS`. Phase 1 uses the sequence for scan codes; row persistence still belongs to Phase 3.

**Deliverables**
- `api/routes_health.py`, `api/routes_scans.py`
- `POST /api/v1/scans` multipart (§13) → persist image to `images/YYYY/MM/DD/SCAN-000001.jpg` (§20) → preprocess → OCR → JSON
- `services/image_service.py` — validation, resize to OCR resolution, orientation handling, grayscale, CLAHE contrast, sharpen; **three** controlled variants only: `original` / `enhanced` / `threshold` (§7 — no combinatorial explosion)
- `services/ocr_service.py` — **the only file that imports paddle.** Returns `list[OcrToken]` with `text`, `bbox`, `confidence` (§6). Spatial data preserved, never flattened to a string
- `models/response_models.py` — Pydantic models matching the §4 JSON shape
- `utils/logging.py` — structured logs with scanId, per-stage durations, failure reasons (§21)
- `tests/` — image_service unit tests; a `curl` example + `scripts/test_client.py` so the pipeline runs without the TC22 (§30)
- Error handling: invalid image → 400, OCR failure → 500 with a safe body (§22)

**Exit gate:** `curl -F image=@sample.jpg .../api/v1/scans` returns tokens with bboxes and confidences; logs show preprocess/OCR/total timings. **Record baseline p95 latency — this decides R8.**

**Est.** 2–3 days

---

## Phase 2 — Extraction, Normalization, Validation, Confidence (§26 P3 + P4)  ✅ DONE (pending real-image tuning)

**Goal:** tokens → the five target fields, normalized, validated, scored. This is where POC success is actually won or lost.

**Verified end to end.** All five fields extracted from the live pipeline at HIGH confidence, overall 0.961. Extraction costs **19 ms** on top of 4.2 s of OCR — the rules layer is free relative to the model. 176 tests pass.

### Findings

**Relative expiry dates are common and were not in the original spec.** Indian FMCG frequently prints `BEST BEFORE 9 MONTHS FROM PACKAGING` instead of an expiry date — there is nothing on the pack to read. Handled by deriving expiry from the manufacturing date plus the printed period, marked `DERIVED_RULE` rather than `OCR_RULES` and flagged ambiguous so it always lands in the operator's review band. This is not fabrication under §11: the pack states the rule, we apply it, and the operator confirms.

**Two bugs caught by testing against realistic layouts, not synthetic ones:**

- Digit-confusion correction was being applied to whole strings, so ordinary words became numbers. `M.R.P. Rs. 20.00 (incl. of all taxes)` contains I, O and S, which translated to 1, 0 and 5 and were read as competing prices. Correction now applies only inside runs that already contain a real digit.
- Overall confidence was the minimum across all five *required* fields, which drove it to zero on any pack missing one. Most real packs lack at least one — a snack pouch prints a batch code and no separate lot code. It is now the minimum across *found* fields, with completeness reported separately as explicit null values.

**Spatial distance penalty was initially far too harsh.** SKU labels print label and value as aligned columns, so wide horizontal gaps are normal layout, not evidence of a bad association. The first constant put every correctly-extracted field into REVIEW, which makes the band useless. Softened, and flagged as provisional — this is exactly the constant §12 says must be tuned against real images.

### Still required before this phase truly closes

Threshold and weight tuning against **real Reliance Retail label images**. The current numbers are engineering defaults validated only against constructed layouts. Until then, treat the confidence figure as directionally useful, not calibrated.

**Deliverables**
- `services/field_extractor.py`
  - Alias table in **config file, not code** (§8) — batch / mfg / exp / lot / mrp, extensible
  - Spatial association (§9): same-line, right-of-label, below-label, distance-weighted. Simple and explainable; comment the heuristics (§28)
- `services/normalizer.py` (§10)
  - Dates → `YYYY-MM` (month/year) or `YYYY-MM-DD` (full). **Ambiguous dates are not silently resolved** — flag for review
  - MRP → decimal
  - Batch/lot → preserve alphanumerics; `O/0 I/1/l S/5 B/8 G/6 Z/2` corrected **only** where the field format justifies it, never globally
- `services/validator.py` (§11) — `mfg < exp`, format checks, allowed charsets, missing → `null`. **Never fabricate a value**
- `services/confidence.py` (§12) — weighted blend of OCR confidence, label-match quality, spatial quality, format validity, cross-field consistency, cross-variant agreement. Bands: `≥0.95 HIGH` / `0.80–0.95 REVIEW` / `<0.80 LOW`. Documented as engineering defaults, **not calibrated probabilities**
- `NO_TEXT_DETECTED` status path (§22) — a valid result, not a 500
- `sample_data/expected.json` ground truth + `tests/test_accuracy.py` producing the **field-level** accuracy table of §24 (per-field accuracy, false accepts, false rejects, no-detection rate, manual-correction rate, mean processing time)
- **No dataset work.** PaddleOCR is used zero-shot. Extraction, normalization, and validation are unit-tested against hand-written OCR token fixtures, so the logic is fully testable before any image exists. The accuracy harness reads `sample_data/*/expected.json` and reports per bucket — it simply has nothing to score until real Reliance Retail images arrive
- Unit tests for date parsing and field extraction (explicitly required by §28)

**Exit gate:** §30 first-runnable-milestone complete end-to-end on the server, plus a printed accuracy report over `sample_data/`. **Second gate, once real Reliance Retail images arrive (R3): re-run and tune thresholds.**

**Est.** 3–4 days

---

## Phase 3 — SQL Server Persistence (§26 P5)  ✅ DONE

**Goal:** durable audit trail sufficient to diagnose any failed scan (§16).

**Verified against live SQL Server.** A scan writes its row, five field rows, and ten OCR token rows in one transaction. Confirm records the operator's values with `WasEdited` derived by comparison, sets `ConfirmedAt`, and moves the scan to `CONFIRMED`. `GET` reads it all back. 196 tests pass.

Full §16 audit trail confirmed present for a real scan:

```text
batchNumber        A23C91      A23C91    A23C92    edited=1
expiryDate         06/28       2028-06   2028-06   edited=0
lotCode            K18P2       K18P2     (null)    edited=1
manufacturingDate  07/26       2026-07   2026-07   edited=0
mrp                Rs. 245.00  245.00    245.00    edited=0
                   ^raw        ^norm     ^confirmed
```

Three value columns are kept deliberately. Collapsing them would destroy the manual-correction rate of §24, which is one of the metrics the POC exists to produce.

### Bug found and fixed: normalization was not idempotent

`/confirm` re-normalizes what the app sends back, and the app sends back the ISO value the server produced. `normalize_date("2026-07")` re-parsed it positionally and read **2026 as the month**, so every confirmed scan picked up a spurious `month out of range` note. The stored value happened to survive via the keep-operator-input fallback, so this would have shipped as quietly corrupted audit data rather than a visible failure.

Fixed with an ISO fast-path; normalization is now idempotent, and that property is under test.

Also suppressed `not found` notes for fields the operator deliberately clears. Clearing a field asserts the label lacks it — information, not a validation problem — and recording it as one would pollute the audit trail of every legitimately short pack.

### Persistence failure policy

Deliberately asymmetric:

| Endpoint | On database failure |
| --- | --- |
| `POST /scans` | Returns results with `persisted: false`. Several seconds of OCR the operator already waited for are not discarded because a write failed. |
| `POST /scans/{id}/confirm` | **500.** This is the commit point; reporting success without storing the confirmation is the worst outcome available (§22). |

Validation problems never block a confirm. The operator is the final authority (§4); refusing their input would leave them unable to record a genuinely odd label. Problems are recorded against the field instead.

**Deliverables**
- `sql/schema.sql` — `SkuScan`, `SkuScanField`, `SkuScanOcr`, optional `SkuMaster` (§15). Single database; no separate OCR store
- `sql/seed.sql` — minimal dev seed
- `db/connection.py` — pyodbc, **ODBC Driver 18**, connection string from env, `Encrypt`/`TrustServerCertificate` explicit
- `db/repositories.py` — insert scan, fields, OCR tokens; fetch scan
- `services/scan_service.py` — orchestrates preprocess → OCR → extract → validate → persist
- `POST /api/v1/scans/{scanId}/confirm` (§13) — server re-validates operator edits before persisting
- `GET /api/v1/scans/{scanId}` (§13)
- DB failure → server error with the scan's processing state left diagnosable (§22)

**Exit gate:** scan → confirm → rows present in `SkuScan` / `SkuScanField` / `SkuScanOcr`, verified via `sqlcmd`; every item in §16 is recoverable for a given scanId.

**Est.** 1–2 days

---

## Phase 4 — Android App (§26 P6)  ✅ DONE — VERIFIED ON TC22

**Goal:** the operator workflow of §4, running on real hardware.

**Built, installed and verified on a real Zebra TC22** (Android 14, SDK 34). Full chain confirmed:

```text
Captured 3060x4080, cropped to 2692x1224 (rotation 90)
POST /api/v1/scans -> 200 OK in 1588ms
SCAN-000048 | NO_TEXT_DETECTED | Zebra Technologies TC22 | 1487ms
```

**R14 is closed.** The shared-`ViewPort` approach produces a correct ROI crop on real optics, and device attribution reaches `SkuScan.DeviceModel`.

Stack: Kotlin + XML views, **no Compose**, light theme only, Material 3.

### Three runtime bugs fixed by reading, since no compiler ran

- **`ViewPort` built from a zero-width preview.** `ProcessCameraProvider`'s listener can fire before `PreviewView` is laid out, and `Rational(0, 0)` throws. Camera binding is now deferred via `previewView.post` with a re-post guard.
- **View geometry read from a background thread.** `cropToRoi` ran on the capture executor and reached into `RoiOverlayView` for the ROI rect. The rect is now read on the main thread and passed into the callback.
- **`Build.SERIAL` for the device id.** Deprecated, and on API 26+ returns the literal string `"unknown"` without `READ_PHONE_STATE` — which would have put a permission prompt in front of the operator to obtain nothing. Replaced with `Settings.Secure.ANDROID_ID`, which needs no permission and is stable per install.

### Screens

Three, not two. `MainActivity` and `ScanActivity` are the two the user asked for; `ResultActivity` is the confirm/edit step, which §27 requires before anything is committed — the flow cannot end at the camera. There is no login, onboarding, or settings screen.

### Design

Palette derived from the Markss mark: ink `#0F1010`, cyan `#00A7E8` sampled from the logo. Cyan is the only saturated colour, so it reads as the action everywhere. Google Sans (OFL, verified before bundling) at 400/500/700, subset to Latin plus `₹` — **168 KB, down from 5.7 MB**.

Dynamic colour deliberately off: confidence bands carry safety meaning and must not be retinted by wallpaper. Each band is signalled three ways — coloured rail, chip background, chip text — so it survives colour blindness and warehouse glare. Fields are sorted **worst-confidence-first**, so the values most likely to be wrong are never below the fold.

### The bug real hardware caught

**The EMDK-based gate would have blocked the TC22 it was written for.**

`com.symbol.emdk.emdkservice` is **not** preinstalled on a TC22 running Android 14 — the EMDK runtime is a separate install. The device carries 115 Zebra and Symbol system packages and none of EMDK. Requiring it would have made the app refuse to run on the only hardware it targets, and the symptom would have been indistinguishable from R11's missing-`<queries>` failure while having an entirely different cause.

The gate now keys off Zebra's MX management framework (`com.symbol.mxmf`), with DataWedge as fallback and EMDK accepted-but-not-required. Each candidate must be installed **as a system package**, which closes the trivial spoof of sideloading an app under a Zebra package name. Verified on device:

```text
I ZebraGate: Zebra device verified: Zebra Technologies TC22 via com.symbol.mxmf
```

### Build errors only a compiler would find

- `settings.gradle.kts` regex escaping collapsed to an illegal Kotlin escape.
- `local.properties` used single backslashes, so Java's properties parser consumed `\U`, `\L`, `\A` as escapes and mangled `sdk.dir`. The build reported only a bare `Invalid file path`. Forward slashes now, with the trap documented in `local.properties.example`.
- `Json.encodeToString` inside a companion object resolved to the member overload expecting a `SerializationStrategy` rather than the reified extension.

### Runtime configuration

Server address and printer Bluetooth MAC moved out of compile-time `BuildConfig` into a settings dialog backed by `SharedPreferences`. A compile-time endpoint is useless in a store where the address changes per site and nobody rebuilds an APK to enter it. An OkHttp interceptor rewrites each request onto the configured host, because Retrofit fixes its base URL at construction and rebuilding the client per settings change would leak connection pools and race with in-flight calls. `BuildConfig` still supplies the default, so no endpoint is hard-coded.

### Repository portability

No keystore, no `signingConfig`, no absolute paths in tracked files, and the build falls back to defaults when `local.properties` is absent — a fresh clone compiles on any machine.

### Still unverified

A real Reliance Retail label through scan → confirm → persist. The pipeline is proven end to end; only OCR accuracy on genuine packaging remains unmeasured.

**Deliverables** — `android/retail-ocr/`
- Kotlin, Gradle wrapper, AGP 8.x + JDK 17, Compose. `targetSdk 35`, `minSdk` set to the TC22's actual API level (see Open Decisions Q1)
- **Zebra-only device gate (§4)** — build this first, it shapes everything after it:
  - `compileOnly 'com.symbol:emdk:+'`, Zebra Maven repo declared under `dependencyResolutionManagement` in `settings.gradle`
  - Manifest `<queries><package android:name="com.symbol.emdk.emdkservice" /></queries>` — **without this the check fails on real Zebra hardware too (R11)**
  - Three-step check, fail closed: `Build.MANUFACTURER` contains `Zebra Technologies` **or** `Motorola Solutions` → `PackageManager` resolves `com.symbol.emdk.emdkservice` → `EMDKManager` initializes
  - Terminal "unsupported device" screen. No degraded fallback
  - `ENFORCE_ZEBRA_ONLY` build-config flag: `false` in debug, `true` in release (R10). Debug bypass logs a loud warning
  - Unit test asserting the `<queries>` element is present in the merged manifest
- Scan screen: CameraX preview + target-area overlay + capture (§4). **EMDK is not in the capture path** — CameraX only
- Lightweight client-side quality checks **only** (§4): readable image, min resolution, Laplacian-variance blur, mean-brightness. No client-side OCR, no vision pipeline
- Retrofit/OkHttp multipart upload; Kotlin data classes mirroring the §4 response
- Processing state screen
- Confirmation screen: every field visible, editable, with confidence styling — HIGH normal / REVIEW warning / LOW strong warning + recapture prompt. **Operator is final authority** (§4)
- Base URL, API key, timeouts from build config / settings — never hard-coded (§19)
- Recapture path on `NO_TEXT_DETECTED`

**Exit gate:** full capture → upload → confirm → persisted, first on SM-S711B, **then re-verified on a real TC22** (R5).

**Est.** 4–5 days

---

## Phase 5 — ZQ320 Printing (§26 P7)

**Goal:** a physical label with a scannable QR.

**Deliverables**
- `interface LabelPrinter { suspend fun print(scan: ConfirmedScan): PrintResult }` (§18) — printer specifics stay behind it; OCR logic stays uncoupled from print logic
- ZQ320-class implementation via Zebra Link-OS SDK over Bluetooth; CPCL/ZPL template per §18 (RELIANCE RETAIL / SKU / Batch / MFG / EXP / LOT / MRP / QR)
- `POST /api/v1/scans/{scanId}/print` + `services/print_service.py` — prepares the payload server-side (§13)
- QR content = compact identifier (`SCAN-000001`), **not** a duplicate of all field data; backend stays source of truth (§17)
- Printer address/config externalized (§19); printer request failures logged (§21)

**Exit gate:** confirmed scan prints a correct label; QR scans back to the right `scanId`.

**Est.** 2 days

---

## Phase 6 — Windows Server / IIS Deployment (§26 P8)

**Goal:** TC22 reaches the server over HTTPS on the real network. **Deliberately last** — §26 says do not start with deployment complexity.

**Deliverables**
- Uvicorn as its own ASGI process behind IIS (§5) — run as a Windows service
- `deployment/iis/web.config` — reverse proxy (ARR/URL Rewrite or HttpPlatformHandler), request size limit sized for JPEG uploads, timeouts sized for OCR latency
- HTTPS certificate installed and bound; TC22 trust configured per Q6
- Image root and SQL connection string set via environment on the server, not in source (§19)
- `deployment/README.md` — reproducible install steps, incl. offline Paddle model placement (R2)

**Exit gate:** TC22 completes a full scan → confirm → print against `https://<server>/api/v1/...` over the site WLAN.

**Est.** 1–2 days

---

## Phase 7 — R&D Tuning & Hardening

**Goal:** turn a working pipeline into a measured one. This is the phase the project actually exists for (§24).

**Deliverables**
- Accuracy runs across all `sample_data/` buckets (good / low_light / blur / glare / curved_packaging / rotated)
- Confidence-threshold tuning against real labels; the §12 defaults are placeholders until this happens
- Bias the tuning toward **safe review over false acceptance** — a wrong expiry date is worse than a recapture request (§24)
- Latency measurements per §23 (upload, preprocess, OCR, extract, DB, end-to-end)
- Optional WebSocket `/ws/v1/scans/{scanId}` (§14) — **only if R8 measurements justify it.** No queue behind it
- Error-handling matrix review against §22
- POC sign-off against the 16 items of §27

**Est.** ongoing, 3+ days

---

## Timeline

```text
P0  Repo skeleton          0.5d   ██
P1  OCR spine              2-3d   ████████
P2  Extraction + scoring   3-4d   ████████████        <- accuracy risk lives here
P3  SQL persistence        1-2d   ████
P4  Android app            4-5d   ██████████████
P5  ZQ320 printing         2d     ██████
P6  IIS deployment         1-2d   ████
P7  Tuning + hardening     3d+    ██████████
                          ~17-22 working days
```

Phases 1–3 have **no external dependencies** and can start now. Phase 4 can begin in parallel with Phase 3 once the Phase 2 response contract is frozen.

---

## Out of Scope (§2 — do not add without an explicit request)

Redis · RabbitMQ · Celery · Kafka · PostgreSQL · vector DBs · LLMs · VLMs · GPU inference · agent frameworks · ML training pipelines · Kubernetes · microservices · cloud infra · distributed processing.

---

## Open Decisions

### Resolved

| # | Question | Resolution |
| --- | --- | --- |
| Q1 | EMDK / DataWedge needed? | **Revised 2026-08-14.** Originally "no". Now: the app must run **only on Zebra hardware**, so EMDK is used for **device gating only** — never for capture, which stays CameraX. DataWedge still unused. **Client-side gate only, no server-side allowlist** — accepted trade-off, see R12. CLAUDE.md §4 rewritten. |
| Q2c | Public datasets | **Dropped 2026-08-14.** PaddleOCR used zero-shot. No fine-tuning, no dataset curation. The project's variable is the extraction layer, not the OCR model. Revisit only if zero-shot recognition turns out to be the bottleneck. |
| Q3 | SQL Server target | **Local `MSSQL$SQLEXPRESS`, Windows auth.** Verified reachable: SQL Server 2022 (RTM) 16.0.1000.6 Express. Schema applied directly. |
| Q2a | Repo layout | **Backend at repository root**, Android in `android/`. CLAUDE.md §5 and §29 updated to match. |
| Q2b | Model download on first run | **Accepted.** Progress state in app + `scripts/warmup_models.py` + startup warmup. Models pre-initialized before any demo. |
| Q4a | QR specification | **10 mm × 10 mm**, carries all confirmed OCR fields, ZPL `^BQ`, Zebra ZSDK over Bluetooth. CLAUDE.md §17 and §18 rewritten. |
| Q8 | PaddleOCR version | **Keep 3.4.1 / paddlepaddle 3.3.1.** See §1a. |

### Still open

Blocking phase noted. None block Phase 0–2.

| # | Question | Blocks |
| --- | --- | --- |
| Q2 | Real Reliance Retail SKU label images — how many, when? English-only or mixed script? (R3, R4) | P2 accuracy gate |
| Q4 | Zebra ZSDK access, and is a ZQ320 available for testing? | P5 |
| Q6 | HTTPS certificate — corporate CA or self-signed? Self-signed means installing trust on every TC22. | P6 |
| Q7 | Is Reliance Retail product master data (`SkuMaster`, §15) available, or is that table deferred? | P3 |

**Defaults applied where unanswered:** Q5 → static API key header (§19 satisfied, isolated from business logic); Q7 → table created, left empty.

---

## Open Findings (2026-08-16)

Things measured or observed during the first end-to-end cycle on real
hardware, to be fixed after the cycle rather than chased mid-measurement.
Per-run measurement output lives in `FINDINGS.md`, which is regenerated by
`scripts/ensemble_accuracy.py`; this list is the hand-kept one.

| # | Finding | Evidence | Severity |
| --- | --- | --- | --- |
| F1 | **The VLM produces plausible wrong values.** On the Eno carton it read mfg `2023-04` and exp `2025-01` against a truth of `2026-05` and `2028-04`, and batch `526L289A` against `S26L289A`. PP-OCRv5 returned nothing on the same pack, which section 24 rates as the *safer* outcome. **Acted on 2026-08-17:** `qwen2.5vl:3b` dropped, `qwen3-vl:4b` installed in its place. Still open — the replacement is unmeasured, and two candidate causes remain unseparated: the model itself, and `vlm_max_side=1024` downscaling a 2692px crop by 2.6× on latency grounds without an accuracy check. | Ground truth vs `qwen2.5vl:3b` transcribe, index 01 | **High** — a false accept is the worst failure class in this project |
| F2 | **A VLM-only field is currently taken at face value.** `ensemble.merge` accepts a field neither corroborated nor contradicted. Given F1 that may need to be agreement-gated, or capped at the REVIEW band. The earlier bake-off found agreement-only gave 2 wrong against 14 for a union. | `app/services/ensemble.py` | **High** |
| F3 | **The readiness gate passed a motion-blurred capture.** Sample index 18 is unreadable and was auto-captured. One in twenty is too many when the gate's whole purpose is to stop bad frames at source. | `sample_data/roi/SAMPLE_20260816_200322_734.jpg` | Medium |
| F4 | **Gate thresholds were tuned against 640×480 frames.** The analysis stream is now 1280×720 and confidence reaches 0.76 where it capped at 0.61, so `GOOD_CONFIDENCE = 0.70` and `TARGET_LINES = 6` are likely too lenient. Probable cause of F3. | `LabelQuality.kt` | Medium |
| F5 | **Code fields sit at 29%**, well below core's 45%. Batch is the weakest field at 25%. Section 24 already treats codes as best-effort with operator entry expected, so this is a question of whether that stays acceptable, not automatically a defect. | First corpus measurement | Low — by design, but worth revisiting |
| F6 | **VLM latency is 100s per image** on this laptop, against 6s for PP-OCRv5. The `fallback` trigger keeps the common case fast, but a pack that needs the VLM will take roughly two minutes. Estimated 25–50s on the 16-core server; unmeasured. | `scripts/ensemble_accuracy.py` | Medium |
| ~~F7~~ | ~~Dangling references left by the corpus reset.~~ **Closed 2026-08-16** — scan tables truncated and the code sequence restarted, so `images/`, the corpus and the database agree again. | — | Closed |
| ~~F5~~ | ~~Code fields sit at 29%.~~ **Improved 2026-08-16** — 41% after the extraction fixes below, with wrong answers down from 6 to 3. Still best-effort by design. | — | Reduced |
| F9 | **A date split across two tokens is unreadable.** `16SEP` and `25` arrive as separate boxes; alone the first normalizes to 2016-09, a plausible-looking year built from the day. Joining adjacent same-line tokens was implemented and measured — it changed nothing, because the pack needing it also needs an association strategy that was itself a net loss (see below). | `SAMPLE_20260816_195938_023.jpg` | Medium |
| F8 | **The detector merges stacked stamp lines into one box, and the recogniser reads one line per box.** On `200214_286` the batch and date stamp is a single 1033×520 detection containing three lines; PP-OCRv5 returns `''`. Splitting it by horizontal projection fails because the pack's own print is double-struck and the lines physically overlap with no gap. | `SAMPLE_20260816_200214_286.jpg` box05 | **High** — this is the shape of the recognition ceiling, not a tuning miss |

### VLM engine swap and resolution test — 2026-08-17

`qwen2.5vl:3b` was replaced because it produced plausible wrong values (F1).
Three things were measured on the way, and two of them killed ideas recorded
earlier in this file.

**`qwen3-vl:4b` is a Thinking model and cannot be used.** All output goes to the
`thinking` field and `response` comes back empty, stopping on `length` while
still reasoning. `think: false` is ignored on both `/api/generate` and
`/api/chat` — the chat template forces it. It spent 130–140s per image
reasoning and never answered. Deleted. **Use `qwen3-vl:4b-instruct`**, the
non-thinking variant.

**Higher resolution makes it worse, not better.** This contradicts the
hypothesis recorded against F1, which assumed `vlm_max_side=1024` was
starving the model. Eno carton, truth `S26L289A` / `2026-05` / `2028-04`:

| `max_side` | mfg | exp | batch | time |
| --- | --- | --- | --- | --- |
| 1024 | `06/2025` | `04/2028` ✓ | `325L289` | 116s |
| 2048 | `05/2023` | `04/2028` ✓ | *(empty)* | 182s |
| 2692 (native) | `05/2023` | `04/2026` | *(empty)* | 369s |

So **that half of F1 is closed: the downscale was not the cause.** Do not
re-raise `vlm_max_side` expecting a gain.

**Per-box crops at native resolution do not work.** This was proposed in the
engine-alternatives section below as the fix that would follow from "the
detector has already localised the hard regions". Fed the five low-confidence
crops from the Eno pack, the model confabulated rather than declining:

```
124x251  ->  '2018年3月'
135x321  ->  '0512643789'
146x253  ->  '5487632109'
125x310  ->  'L-15023478960'
 86x89   ->  '3012547896'
```

Invented digit runs, one in Chinese, at ~80s each. The crops are the tall
vertical fragments so the input is unfair, but a model that fabricates instead
of returning nothing is worse than one that misreads — section 24 again. The
recommendation in the engine-alternatives section is **withdrawn** in this
form; a mid-sized region crop is untested and may still be worth trying.

**And then the whole-image result overturned the plan.** Scored on the same six
images, through the same ground truth and the same scorer, the VLM alone —
no PP-OCRv5 anywhere in the path — beats the shipping pipeline outright:

| engine | core | codes | wrong | missed | per image |
| --- | --- | --- | --- | --- | --- |
| PP-OCRv5 + rules | 44% | 17% | 1 | 13 | 6s |
| `qwen3-vl:4b-instruct` alone, JSON mode | **62%** | **67%** | 8 | **0** | 112s |

Two cautions attach to that, and neither is small:

- **It never declines.** Zero missed against thirteen. Every field gets an
  answer whether or not the pack was legible, so its failures are invisible
  where PP-OCRv5's are obvious. Section 24 rates a wrong value above a missing
  one, which makes F2 — capping a VLM-only field into REVIEW — a prerequisite
  rather than a refinement.
- **112s against 6s.** Unusable on the laptop; the 16-core server is the test
  that matters.

A prior conclusion in this file, that the VLM is "a second engine, not a
replacement", is now the open question rather than the answer. Full-corpus run
pending before anything is decided.

### Extraction fixes — 2026-08-16

Nine values sat in recognised text that extraction was not pulling. Seven fixes
went in; two were measured and reverted. Result on the 20-image corpus:

| | core | codes | wrong | false accepts |
| --- | --- | --- | --- | --- |
| Before | 45% | 29% | 12 | 0 |
| After | **51%** | **41%** | **7** | 0 |

Two were structural bugs rather than missing rules, and both were invisible
from the accuracy number alone:

- **`_infer_stamp_batch` was unreachable on any pack printing two dates.** An
  early `return` in the two-date branch of `_infer_unlabelled` skipped it — so
  the most common stamp layout there is was exactly the one whose batch code
  was never inferred.
- **A stamped time counted as a date.** `13:02:38` reads as a valid 2038 date
  inside the plausible window, making a two-date pack look like a three-date
  pack and silently disabling the rule that arbitrates between them.

The rest:

- **Both date labels resolving to one token.** On a skewed capture the label
  column drifts down relative to the value column, so "Use By" is nearer the
  manufacturing row than its own, and the real expiry sits unclaimed. Now
  ordered by the section 11 rule — earlier is manufacture. Not a general
  exclusivity rule; that was tried previously and cost two core points.
- **Separators recognised as digits are invisible to `_DATE_TOKEN`.** It
  insists on a real separator, so `1610612026` never reached the normalizer
  that knows how to repair it.
- **Statutory numbers adopted as batch codes.** `Lic. No. 1001` shared a row
  with a `B.NO.` label. A token carrying its own label is never a bare value.
- **Garbled codes tidied instead of rejected.** `normalize_code` deleted the
  CJK glyph in `L三F.F0.359` and returned a clean-looking `LFF0359`. For a code
  field, section 24 rates a missing value above a wrong one.
- **Footnote markers.** `#RC-DS5720` failed the first character of the stamp
  code pattern.
- **`190/-` printed as `1901-`.** The slash-as-1 confusion again, this time in
  the price marker. Treated as weak evidence needing corroboration, since
  unlike a real `/-` the marker is inferred.

**Measured and reverted.** A "next row and to the right" association strategy,
for packs whose value column is offset downward as well as across. It won one
code field and converted two core misses into two core wrongs — the wrong
direction under section 24. Joining a date split across adjacent tokens went in
alongside it and, measured on its own, changed nothing (see F9).

### Engine alternatives — investigated 2026-08-16, no change made

The question was whether to replace PP-OCRv5 with Tesseract, Surya, EasyOCR or
docTR. The measurement says **no**, and reframes the problem.

**Detection is not the failure.** Across the 20-image corpus PP-OCRv5 produces
284 boxes and the missing values *are* among them. 12% of boxes come back below
0.50 confidence, several as empty strings. Every full-stack alternative would
discard a detector that works to replace a recogniser that does not.

Four things were tested against that recogniser. Three were free and two of
them were dead ends:

| Experiment | Result |
| --- | --- |
| Re-crop each bad box from the **full-resolution original** instead of the 960px downscale | **+0 values.** The downscale is not the cause. |
| **Morphological close** to bridge inkjet dot matrix into strokes | **+0 values.** Not a preprocessing problem. |
| **Rotate tall boxes 90°** and re-read | Fires on 11 tokens, some from nothing to 1.00 confidence — but every rescued string is boilerplate. **+0 core points.** See `ocr_retry_rotated_boxes`. |
| Split merged multi-line boxes (F8) | Failed — the pack's print overlaps with no gap to split on. |

Standing assessment of the alternatives, none installed:

- **Tesseract** — LSTM trained on rendered clean fonts; dot-matrix is its
  textbook failure mode. Expect worse than PP-OCRv5.
- **EasyOCR** — same CRNN family as PP-OCRv5's recogniser. ~3GB of torch for
  marginal odds.
- **docTR** — Apache-2.0, and its `parseq` backbone is genuinely stronger on
  irregular text. Best of the classical set if one is tried.
- **Surya** — **check the licence before spending any time.** It carries a
  commercial-use revenue restriction and Reliance Retail is far above any small-business
  threshold. Also CPU-slow.

**Consequence for the VLM stage.** The failing regions are already localised by
a working detector, so the VLM should be fed **per-box crops at native
resolution**, not the whole image at `vlm_max_side`. That directly addresses F1
— the downscale I chose on latency grounds and never checked for accuracy — and
replaces one 100s whole-image pass with a few small ones.

### Closed during this cycle

- Deskew direction verified on device by the user.
- ZQ320 printing confirmed end to end, including the QR.
- The full chain runs on real hardware: capture → OCR → confirm → SQL Server → print.

---

## Pivot Policy

This is stage one. Approaches here are engineering defaults, not commitments.

When something changes — a better OCR configuration, a different extraction strategy, a scope change from Reliance Retail — **update `CLAUDE.md` and this file in the same change that alters the code.** Record why. A spec that drifts from the implementation is worse than no spec.

`CLAUDE.md` §2 (out of scope) and §24 (accuracy goals) are the two sections to challenge first if the POC stalls.
