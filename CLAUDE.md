# CLAUDE.md — Reliance Retail SKU Label OCR Application

## 1. Project Overview

Build a server-backed Android application for Reliance Retail that captures images of SKU/product labels using a Zebra TC22, sends the image to a Windows server, extracts printed label information using CPU-based OCR, returns structured data to the Android app for operator confirmation, stores the confirmed data in Microsoft SQL Server, and prints a QR code/label using a Zebra ZQ320-class mobile printer.

This is an R&D/POC implementation intended to demonstrate the complete end-to-end workflow.

### Primary use case

The operator points the TC22 camera at a product/SKU label and captures an image containing information such as:

- SKU Code
- Batch number
- Manufacturing date
- Expiry date
- Lot code
- MRP
- Other useful printed label text

The system must extract these values, present them to the operator for confirmation/editing, persist the confirmed result, and print a QR code/label.

### Document status

This is a living document, not a frozen specification.

The project is at its initial stage. Approaches described here may be pivoted as real label images, real TC22 behaviour, and measured accuracy come in. When a decision changes, **update this file** rather than leaving code and spec out of sync. Record the reason for the change so the pivot is auditable.

Sections 2 (out of scope) and 24 (accuracy goals) are the two that should be challenged before any other. Everything else is an engineering default.

---

## 2. Current Scope — Do Not Add Unrelated Infrastructure

This build is intentionally simple and CPU-only.

### Required

- Zebra TC22 Android device
- Kotlin Android application
- CameraX for image capture
- HTTP/REST communication between Android and server
- Optional WebSocket support for processing status/progress if needed
- Python backend
- FastAPI
- OpenCV for image preprocessing
- PaddleOCR / PP-OCR for OCR
- Python-based rule/regex/spatial field extraction
- Microsoft SQL Server
- Zebra ZQ320-class mobile printer integration
- QR code generation
- Windows Server hosting
- IIS as the external web/reverse-proxy layer

### Explicitly out of scope for this build

Do not introduce the following unless the user explicitly asks later:

- Redis
- RabbitMQ
- Celery
- Kafka
- PostgreSQL
- Vector databases
- LLMs
- GPU inference
- AI agent frameworks
- Machine-learning training pipelines
- Kubernetes
- Microservice decomposition
- Cloud-specific infrastructure
- Complex distributed processing

Keep the implementation small, understandable, and suitable for a fast R&D POC.

### Pivot, 2026-08-16 — a vision-language model is now in scope

Vision-language models were on the excluded list above. They are no longer,
because the exclusion assumed PP-OCRv5 could read the labels and it cannot read
all of them.

Measured on a TC22 capture of an Eno carton: the pack's **printed** field names
came back at 0.95–0.99 confidence, and the **inkjet values** beside them at
0.05–0.53, as `Phm`, `Voow`, `JAND`, `4000`. Those values are 218–291 px tall in
the original, so it is not a resolution problem. All three PP-OCRv5 recognition
models (`PP-OCRv5_mobile_rec`, `en_PP-OCRv5_mobile_rec`, `PP-OCRv5_server_rec`)
were tried and none read them. `scripts/extraction_headroom.py` puts the whole
engine's ceiling at **66%** even with a perfect extractor, against the 95% target
in section 24.

The engine is a small Qwen vision-language model run **locally on CPU through
Ollama** — no cloud, no GPU, no cost, which keeps the constraints of this
section intact.

**Correction, 2026-08-17.** This section previously claimed `qwen2.5vl:3b`
"reads that stamp". It does not. Checked against hand-transcribed ground truth
it returned mfg `2023-04`, exp `2025-01` and batch `526L289A` where the pack
prints `2026-05`, `2028-04` and `S26L289A`. The original claim came from
comparing its output to PP-OCRv5's garbage rather than to the pack. Under
section 24 a confidently wrong value is worse than a missing one, so this is a
reason to change engine rather than to tune around it.

The current model is **`qwen3-vl:4b-instruct`**. The `-instruct` suffix is load
bearing: the plain `qwen3-vl:4b` tag is the Thinking variant, which puts every
token in the `thinking` field, returns an empty `response`, and ignores
`think: false` on both Ollama endpoints.

**Measured 2026-08-17 on the 20-image gated corpus**, scored through the same
ground truth and the same scorer as the primary engine:

| engine | core | codes | wrong | missed | false accepts | per image |
| --- | --- | --- | --- | --- | --- | --- |
| PP-OCRv5 + rules | 51% | 41% | 7 | 29 | 0 | 6.1s |
| `qwen3-vl:4b-instruct` alone | **70%** | **65%** | 18 | 4 | **2** | 94.6s |

Rules that still apply:

- Its output goes through the **same** extraction, normalization, validation and
  confidence path, so its accuracy is directly comparable.
- It stays **off by default** until the failure profile below is handled.
- Everything else on the excluded list above stays excluded.

**"A second engine, not a replacement" is now under review.** The VLM alone
beats the full PP-OCRv5 pipeline by 19 points on core and 24 on codes, which is
the opposite of what this section assumed. But it does so by never declining:
4 missed against 29, and it produced this project's first two false accepts.
That trade is not free under section 24, where a wrong value outranks a missing
one, so before the VLM can carry a field on its own a VLM-only value must be
capped into the REVIEW band rather than presented as HIGH.

GPU inference, cloud APIs and any training or fine-tuning remain out of scope.

---

## 3. High-Level Architecture

```text
+----------------------+       HTTPS REST        +---------------------------+
|     Zebra TC22       | ----------------------> | Windows Server            |
|                      |                          |                           |
| Kotlin Android App   |                          | IIS                       |
| CameraX              |                          |   |                       |
| Confirmation UI      | <---------------------- | FastAPI / Uvicorn         |
| Printer Integration  |       JSON / status     |   |                       |
+----------------------+                          |   +-- OpenCV              |
                                                  |   +-- PaddleOCR            |
                                                  |   +-- Field Extraction    |
                                                  |   +-- Validation           |
                                                  |   +-- SQL Server          |
                                                  +---------------------------+
                                                              |
                                                              v
                                                      +---------------+
                                                      | SQL Server    |
                                                      +---------------+

TC22 -------------------------------------------------> ZQ320
                 confirmed data / print command
```

### Important architectural rule

The TC22 is a capture/client device. Heavy image processing and OCR must happen on the server.

The Android application must not perform the primary OCR workload.

---

## 4. Android Application Requirements

### Platform

- Kotlin
- Android application
- Zebra TC22
- Use CameraX for image capture

### Zebra Android SDKs

| SDK | Used for | Not used for |
| --- | --- | --- |
| **EMDK for Android** | Device gating only — proving the app is running on a Zebra device | Camera, capture, scanning |
| **ZSDK (Link-OS)** | ZQ320 printing over Bluetooth (section 18) | Anything else |
| **DataWedge** | Nothing. Not used. | — |

Image capture is driven by **CameraX alone**. Do not route label imaging through EMDK, DataWedge, or any Zebra capture middleware.

### Device restriction — Zebra only

The application must run **only on Zebra hardware, targeting the TC22**. It must refuse to operate on a non-Zebra Android device.

EMDK 13.0 supports TC21, TC22, TC27, TC53, and TC73 on Android 13.

#### Client-side gate

Check on launch, in this order, and fail closed:

1. `Build.MANUFACTURER` contains `"Zebra Technologies"` or `"Motorola Solutions"` — older Zebra units are rebranded Motorola Solutions, so both strings must be accepted.
2. `PackageManager` resolves `com.symbol.emdk.emdkservice`.
3. `EMDKManager` initializes successfully.

On failure, show a terminal "unsupported device" screen. Do not allow the scan flow to start, and do not fall back to a degraded mode.

**Android 11+ package-visibility requirement.** The manifest must declare:

```xml
<queries>
    <package android:name="com.symbol.emdk.emdkservice" />
</queries>
```

Without this, the `PackageManager` lookup silently returns "not found" on **every** device, including genuine Zebra hardware, and the gate rejects everything. This is the most common way this check is implemented wrong.

#### Gradle dependency

```gradle
compileOnly 'com.symbol:emdk:+'
```

EMDK is **`compileOnly`**. The runtime ships on the Zebra device; the AAR is compile-time only. Consequences that must be understood:

- The APK still builds and installs on a non-Zebra device. The restriction is enforced at runtime, not by the build.
- The dependency resolves from Zebra's Maven repository, which must be declared under `dependencyResolutionManagement` in `settings.gradle` for current Gradle versions.

#### Build-variant escape hatch — required

The gate must be driven by a build-config flag, not hard-coded:

```text
debug   -> ENFORCE_ZEBRA_ONLY = false
release -> ENFORCE_ZEBRA_ONLY = true
```

Without this, no development or testing is possible on any machine until a TC22 is physically available. The debug bypass must log a loud warning and must never be enabled in a release build.

#### Enforcement boundary

The gate is **client-side only**. There is no server-side device allowlist.

This is a deliberate, accepted decision. The check is a runtime check, so the APK can still be installed on non-Zebra hardware and `Build.MANUFACTURER` is spoofable on a rooted device. Neither matters here: the app is useless off a TC22 because the scan flow refuses to start, and the deployment is a controlled store estate rather than a public distribution.

Do not add server-side device validation unless the user asks for it.

### Region of interest — automatic, never a manual crop

The scan screen shows a fixed **ROI window** overlaid on the camera preview, with instruction text above it:

```text
Place the desired section inside this window
```

The app crops the captured frame to that window and uploads **only the cropped region**.

Rules:

- **The operator never crops.** There is no post-capture editing step, no drag handles, no confirm-crop screen. Adding one would be a second task per scan for no benefit — the operator has already framed the label.
- The ROI is a fixed rectangle. The operator aligns the label to it while framing, exactly as they already do with a barcode reticle.
- The crop happens on-device before upload. The backend never sees the surrounding packaging.

Why this matters beyond usability: OCR latency scales with the **number of detected text regions**, not image size. A full pack shot carries ingredients, addresses, statutory text, and barcodes, all of which get detected and recognized at real cost. Cropping to the ROI is the single largest latency lever in the system.

**Coordinate-mapping trap.** The preview surface and the captured image have different resolutions, and often different aspect ratios. Using preview pixel coordinates directly against the full-resolution capture crops the wrong region — usually subtly wrong, which is worse than obviously wrong. Bind the preview and capture use cases to a shared `ViewPort` in a `UseCaseGroup` so CameraX applies a consistent crop rect, and verify the mapping on the TC22 rather than assuming it.

### Camera workflow

The app should provide a simple scan screen:

1. Show camera preview with the ROI window overlaid.
2. Guide the operator to place the SKU label inside the ROI window.
3. Capture an image and crop it to the ROI.
4. Perform only lightweight client-side image-quality checks where practical.
5. Upload the image to the backend.
6. Show processing state.
7. Receive extracted fields.
8. Present the extracted values in a confirmation/edit screen.
9. On operator confirmation, persist the data through the backend.
10. Trigger printing.

### Client-side image quality checks

Useful checks:

- Image exists and is readable.
- Minimum resolution.
- Basic blur detection if practical.
- Basic brightness/darkness check if practical.
- Image is not obviously empty/invalid.

Do not over-engineer this for the first POC.

#### On-device text detection — ML Kit

**Pivot, 2026-08-16.** This section previously said "do not build an AI vision
pipeline on the TC22". That is now qualified: **ML Kit text recognition runs on
the preview stream to judge capture quality, skew and framing.**

Dependency, and the reason for the exact artifact:

```gradle
implementation 'com.google.mlkit:text-recognition:16.0.1'
```

Bundled, **not** `com.google.android.gms:play-services-mlkit-text-recognition`.
Zebra ships the TC22 in GMS and AOSP variants, and the play-services artifact
does not work at all on an AOSP unit. Bundled costs about 4MB and has no such
dependency. Latin only — the fields are alphanumeric.

**What it is used for, and what it is not.** Measured on the 15 ROI captures,
scored through the server's own extractor and ground truth:

| Engine | Core | Codes | Per image |
| --- | --- | --- | --- |
| `PP-OCRv5_mobile_det` + `PP-OCRv5_mobile_rec` + rules (server) | 43% | 38% | 4.3s |
| ML Kit + rules (on device) | 29% | 12% | **252ms** |

So it is **not** a replacement for server OCR — its recognition ceiling is 50%
against PP-OCRv5's 66%. It is used for the thing it is genuinely better at:
250ms is fast enough to run continuously on the preview, which buys three
things the server architecture structurally cannot:

- **Capture gating.** Text present in the ROI, a field label visible, sane
  skew — checked before the shutter fires. Every accuracy figure in this
  project is measured on frames an operator already chose to keep, and nothing
  used to check whether keeping them was a good idea.
- **Deskew.** `Text.Line.getAngle()` gives the printed rotation, so the crop is
  straightened on-device without OpenCV.
- **A second opinion.** Its tokens are complementary — it read values PP-OCRv5
  never recognised — and at 252ms it is nearly free to consult.

**The gate arms the shutter. The operator fires it.**

Two separate powers, split deliberately.

**Arming is the gate's job.** The capture button is disabled until the smoothed
band reaches GOOD. A frame too poor to read costs a round trip to the server
and comes back as a failed scan, so refusing it on the device is cheaper for
the operator than letting it through. This is also the only thing in the system
that checks whether a frame was worth keeping — every accuracy figure in this
project is measured on frames an operator chose, and nothing else audits that
choice.

**Firing is the operator's.** The app must not capture by itself. An earlier
build auto-captured once the score had held green for a 600ms dwell, reasoning
that the frame knows better than human reaction time when it is worth keeping.
That was wrong about who is in charge: the operator can see what the score
cannot — that this is the wrong face of the pack, that a hand is about to move,
that they are simply not ready. It also kept a motion-blurred frame in the
gated corpus that no person would have chosen (`PLAN.md` F3).

**Correction, 2026-08-17.** This section previously said the gate "never
disables the capture button", on the reasoning that a gate which refuses to
fire is indistinguishable from a broken app. That concern is real but it is
handled by the hint text, which always says what is wrong and what to do about
it — "Move closer", "Straighten the label", "Hold steady". A disabled button
beside a specific instruction is not a mystery. Letting unreadable frames
through to be diagnosed on the server is the worse trade.

Primary extraction stays on the server, so the rule in section 3 still holds.

#### Cropping and scaling — no third-party library

Use the platform. `BitmapRegionDecoder` decodes only the region asked for
without materializing the full bitmap, `BitmapFactory.Options.inSampleSize`
downscales at decode time, and `Bitmap.createBitmap(src, x, y, w, h, matrix,
true)` crops, rotates and scales in one pass.

Do not add uCrop or CanHub Android-Image-Cropper — both are interactive crop
UIs, and section 4 rules out manual cropping. Do not add OpenCV to the Android
project for cropping; it is 40–70MB of native libraries for work the platform
already does. Revisit only if true perspective correction on curved packs is
needed, and measure before adding it.

### Image upload

Prefer multipart HTTP upload:

```http
POST /api/v1/scans
Content-Type: multipart/form-data
```

Field:

```text
image=<JPEG image>
```

The server returns a scan identifier and processing result.

If processing time requires asynchronous updates, add a WebSocket connection for status updates. Do not introduce a message queue.

### Android result model

Use a structured response similar to:

```json
{
  "scanId": "SCAN-000001",
  "status": "COMPLETED",
  "overallConfidence": 0.96,
  "fields": {
    "batchNumber": {
      "value": "A23C91",
      "confidence": 0.96,
      "source": "OCR_RULES"
    },
    "manufacturingDate": {
      "value": "2026-07",
      "confidence": 0.99,
      "source": "OCR_RULES"
    },
    "expiryDate": {
      "value": "2028-06",
      "confidence": 0.98,
      "source": "OCR_RULES"
    },
    "lotCode": {
      "value": "K18P2",
      "confidence": 0.91,
      "source": "OCR_RULES"
    },
    "mrp": {
      "value": "245.00",
      "confidence": 0.99,
      "source": "OCR_RULES"
    }
  }
}
```

The exact JSON naming can be adjusted consistently across Android and backend, but the concept must remain the same.

### Confirmation UI

Display each extracted field clearly.

The operator must be able to:

- Review the value.
- Edit incorrect values.
- Confirm the complete result.

Recommended visual distinction:

- High confidence: normal/positive state.
- Medium confidence: warning/review state.
- Low confidence: strong warning and preferably request a recapture.

The operator must be the final authority before data is committed.

---

## 5. Backend Requirements

### Technology

- Python 3.x
- FastAPI
- Uvicorn (or equivalent ASGI server)
- OpenCV
- PaddleOCR / PP-OCR
- Pydantic
- Microsoft SQL Server connectivity

### Windows deployment

The server environment is expected to be Windows Server.

IIS should be used as the externally exposed web server/reverse-proxy layer.

The Python/FastAPI application should run as its own Python/ASGI process behind IIS.

Do not make the application depend on Linux-only functionality.

### Backend project structure

The backend is rooted at the repository root, not in a `server/` subfolder. Use a clean modular structure similar to:

```text
project-root/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── api/
│   │   ├── routes_scans.py
│   │   ├── routes_health.py
│   │   └── routes_print.py
│   ├── models/
│   │   ├── request_models.py
│   │   └── response_models.py
│   ├── services/
│   │   ├── image_service.py
│   │   ├── ocr_service.py
│   │   ├── field_extractor.py
│   │   ├── normalizer.py
│   │   ├── validator.py
│   │   ├── confidence.py
│   │   ├── scan_service.py
│   │   └── print_service.py
│   ├── db/
│   │   ├── connection.py
│   │   └── repositories.py
│   └── utils/
│       ├── logging.py
│       └── image_utils.py
├── tests/
├── sql/
│   └── schema.sql
├── requirements.txt
└── README.md
```

The exact organization may differ slightly, but OCR, extraction, validation, database access, and API routing must remain separated.

---

## 6. OCR Pipeline

The primary OCR engine for the POC is PaddleOCR / PP-OCR running on CPU.

Do not use GPU-specific code.

### Pinned versions

```text
paddleocr    3.4.1
paddlepaddle 3.3.1   (CPU build)
```

This is the **3.x** line, which is the line that ships PP-OCRv5. Do not downgrade to the 2.x line.

The 3.x API differs from the 2.x examples found in most tutorials. Confirmed differences:

- `use_angle_cls` no longer exists. Use `use_textline_orientation`.
- `predict()` is the current inference entry point.
- Models are selected by name: `text_detection_model_name`, `text_recognition_model_name`.
- The pipeline version is selected with `ocr_version`.
- Detection and recognition thresholds are constructor arguments: `text_det_limit_side_len`, `text_det_limit_type`, `text_det_thresh`, `text_det_box_thresh`, `text_rec_score_thresh`.

Treat any PaddleOCR code sample that uses `use_angle_cls` as out of date.

### Model selection

PP-OCRv5 is the target pipeline. Published CPU benchmarks:

| Variant | Det accuracy | Rec accuracy | CPU time / image |
| --- | --- | --- | --- |
| mobile det + mobile rec | 0.770 | 0.8015 | ~1.75 s |
| server det + server rec | 0.827 | 0.8401 | ~4.34 s |

Start with **`PP-OCRv5_server_det` + `PP-OCRv5_mobile_rec`**: detection quality dominates on small dense label print, while recognition on short alphanumeric fields is the cheaper place to trade accuracy for speed.

Both model names must be configurable so all four combinations can be A/B tested against real label images. Do not hard-code a model name in service code.

### The ceiling is recognition, not detection — measured 2026-08-16

Before changing OCR engine, know which half is failing. It is not the half most
people assume.

On the 20-image gated corpus PP-OCRv5 produces **284 detection boxes, and the
values it gets wrong are among them**. 12% of boxes come back below 0.50
confidence, several as empty strings. On `SAMPLE_20260816_200214_286` the batch
stamp is the *largest box in the image* and recognition returns `''`.

Consequences, and they are binding:

- **Do not replace the whole engine.** Tesseract, Surya, EasyOCR and docTR are
  full-stack detectors *and* recognisers. Swapping to one discards a detector
  that works to fix a recogniser that does not. If an alternative is tried, try
  it as a **recogniser only**, on the boxes PP-OCRv5 already flagged as bad.
- **Check Surya's licence before spending time on it.** It carries a
  commercial-use revenue restriction that Reliance Retail is far above.
- **Feed the VLM per-box crops at native resolution**, not the whole image at
  `vlm_max_side`. The detector has already localised the hard regions.

Three cheap fixes were measured and **all won zero core points**: re-cropping
bad boxes at full resolution, morphological closing to bridge inkjet dots, and
rotating tall boxes upright. See `PLAN.md` → Engine alternatives. Do not re-run
them on this corpus expecting a different answer.

### Built-in preprocessing stages

The 3.x pipeline exposes two stages that overlap with the OpenCV work in section 7:

- `use_doc_orientation_classify` — page orientation
- document unwarping (`doc_unwarping_model_name`) — useful for curved packaging

Evaluate these before writing custom OpenCV rotation and perspective correction. Do not build by hand what the pipeline already does, but do measure the latency cost of enabling them.

### Model download and warmup

PaddleOCR downloads model files on first use. This is acceptable.

Requirements:

- Model directory must be configurable and point at a vendored `models/` path in the deployed environment.
- Provide a warmup script that downloads and initializes all models ahead of time, so a demo never pays the download cost.
- The backend must run a warmup inference at startup so the first real scan is not slowed by lazy initialization.
- If a download is in progress, the Android app should show a clear progress or "preparing OCR" state rather than appearing to hang.

### Processing pipeline

```text
Uploaded image
      |
      v
Validate image
      |
      v
OpenCV preprocessing
      |
      v
PaddleOCR
      |
      +---- text
      +---- bounding boxes
      +---- OCR confidence
      |
      v
Field extraction
      |
      v
Normalization
      |
      v
Cross-field validation
      |
      v
Confidence calculation
      |
      v
Structured JSON
```

### OCR output must preserve spatial data

Do not reduce OCR output to a plain concatenated string.

Retain:

- recognized text
- bounding box
- OCR confidence
- page/image reference where applicable

This is important because field extraction can use spatial relationships between labels and values.

Example internal OCR record:

```json
{
  "text": "BATCH NO",
  "bbox": [120, 240, 310, 280],
  "confidence": 0.98
}
```

---

## 7. Image Preprocessing

Use OpenCV for practical preprocessing before OCR.

Start with:

1. Image validation
2. Resize to a suitable OCR resolution
3. Orientation/rotation handling where practical
4. Perspective correction where practical
5. Grayscale conversion
6. Contrast enhancement
7. Sharpening
8. Optional denoise
9. Optional thresholding

Do not assume one preprocessing method is always best.

For R&D, it is acceptable to test multiple variants of the same image and compare OCR results.

Example:

```text
original
   |
   +----> OCR
   |
   +----> enhanced OCR
   |
   +----> thresholded OCR
```

Do not run an uncontrolled explosion of preprocessing combinations. Keep the POC measurable and simple.

---

## 8. Field Extraction

This is not a generic document-understanding problem. The target is a small set of known SKU-label fields.

Primary fields:

- `batchNumber`
- `manufacturingDate`
- `expiryDate`
- `lotCode`
- `mrp`

The design should allow additional fields to be added later.

### Label aliases

The extractor should recognize common aliases.

Examples:

```text
Batch:
BATCH
BATCH NO
BATCH NO.
B.NO
B.NO.
BATCH NUMBER

Manufacturing:
MFG
MFD
MFG DATE
MFD DATE
MANUFACTURING
MANUFACTURED
PACKED
PKD

Expiry:
EXP
EXP DATE
EXPIRY
EXPIRY DATE
USE BEFORE
BEST BEFORE

Lot:
LOT
LOT NO
LOT NO.
LOT CODE

MRP:
MRP
M.R.P
M.R.P.
MAX RETAIL PRICE
```

These are starting rules only. Keep them configurable and easy to extend. They live in `config/field_aliases.yaml`, never in code.

### Product scope

Reliance Retail SKU means the **full retail range** — snacks (Lays, Pringles), detergents (Surf Excel), packaged foods, household goods, personal care. Do not tune the extractor around one category.

Practical consequences:

- Layouts vary from columnar printed labels to inkjet dot-matrix stamps on flexible film.
- Substrates vary: rigid cartons, metallized snack film (glare), curved cans and bottles, crinkled pouches.
- Not every pack carries every field. A snack pouch typically prints a batch code and no separate lot code. Missing fields are normal, not failures.

### Relative expiry dates

Many Indian FMCG packs print a **shelf life instead of an expiry date**:

```text
BEST BEFORE 9 MONTHS FROM PACKAGING
BEST BEFORE NINE MONTHS FROM THE DATE OF MANUFACTURE
USE WITHIN 6 MONTHS OF MFG
```

There is no expiry date on the pack to read. Derive it from the manufacturing date plus the printed period.

This does not violate the "never fabricate" rule of section 11 — the pack states both the rule and the manufacturing date, and the rule is simply applied. But it must be:

- marked `DERIVED_RULE`, never `OCR_RULES`
- flagged so confidence drops into the review band
- always confirmed by the operator

If the manufacturing date is missing, do not derive. Return the expiry as not found.

---

## 9. Spatial Field Association

Whenever possible, use OCR bounding boxes to associate a field label with its value.

Example:

```text
BATCH NO       A23C91
MFG            07/26
EXP            06/28
LOT            K18P2
```

If `BATCH NO` is detected, look for the nearest plausible value in the expected spatial direction (same line, right side, or nearby below) instead of searching the complete OCR text blindly.

The implementation should support:

- same-line association
- right-of-label association
- below-label association
- distance-based matching

Keep the initial heuristics simple and explainable.

---

## 10. Value Normalization

Normalize extracted values into consistent server-side formats.

### Dates

Possible source formats include:

```text
07/26
07-26
07/2026
07-2026
07/07/26
07/07/2026
07-07-26
07-07-2026
```

Normalize according to field meaning and available context.

For month/year values, use:

```text
YYYY-MM
```

For full dates, use a consistent ISO-style representation:

```text
YYYY-MM-DD
```

Do not silently convert an ambiguous date when the interpretation cannot be justified.

### MRP

Normalize to a numeric decimal representation where possible.

### Batch/Lot

Preserve significant alphanumeric characters.

Be careful with OCR confusion between:

```text
O / 0
I / 1 / l
S / 5
B / 8
G / 6
Z / 2
```

Do not blindly replace characters globally. Apply contextual correction only when the field format makes it reasonable.

---

## 11. Validation

Validation must be deterministic and must happen after extraction.

### Date consistency

When both dates are available:

```text
manufacturingDate < expiryDate
```

If this rule fails, mark the result for review instead of silently accepting it.

### Format validation

Examples:

- MRP should be numeric.
- Dates must match an accepted pattern.
- Batch/lot should contain an allowed character set.
- Required fields should be explicitly marked missing when they cannot be found.

### Missing/ambiguous values

Do not invent values.

Return `null`/missing where extraction fails.

The Android UI must allow the operator to correct these values manually.

---

## 12. Confidence Scoring

Do not expose raw OCR confidence as the only confidence measure.

Build an application-level confidence score using available signals such as:

- OCR recognition confidence
- Field-label match quality
- Spatial association quality
- Format validation
- Cross-field consistency
- Agreement between OCR preprocessing variants, if multiple variants are used

Example:

```text
finalConfidence = weighted combination of the above signals
```

Keep this logic transparent and easy to tune.

Do not claim that an arbitrary confidence number is a statistically calibrated probability.

Suggested initial UI bands:

```text
>= 0.95    HIGH
0.80-0.95  REVIEW
< 0.80     LOW / RECAPTURE
```

These thresholds are initial engineering defaults and must be tuned against real label images.

---

## 13. REST API

Implement a small versioned API.

### Health

```http
GET /api/v1/health
```

Response:

```json
{
  "status": "UP"
}
```

### Create scan / OCR image

```http
POST /api/v1/scans
Content-Type: multipart/form-data
```

Input:

```text
image=<JPEG/PNG>
```

Response should include:

- scan ID
- processing status
- extracted fields
- confidence
- OCR metadata needed by the UI/debugging workflow

### Confirm scan

```http
POST /api/v1/scans/{scanId}/confirm
```

Body should contain the operator-confirmed/edited fields.

The server should validate and persist the final values.

### Print

```http
POST /api/v1/scans/{scanId}/print
```

This endpoint should trigger/prepare the printer payload for the confirmed scan.

The exact printer transport implementation may be kept behind `print_service.py`.

### Optional status endpoint

```http
GET /api/v1/scans/{scanId}
```

Useful if scan processing is asynchronous.

---

## 14. WebSocket

WebSocket is optional, not mandatory for the first synchronous POC.

Use it only if actual OCR processing time makes progress/status feedback useful.

Suggested endpoint:

```text
/ws/v1/scans/{scanId}
```

Possible messages:

```json
{
  "type": "PROCESSING",
  "message": "Running OCR"
}
```

```json
{
  "type": "COMPLETED",
  "scanId": "SCAN-000001"
}
```

Do not add a queueing system behind WebSocket.

---

## 15. SQL Server

Use Microsoft SQL Server as the only database for this build.

Suggested starting schema:

### `SkuScan`

```text
Id                  uniqueidentifier / GUID
DeviceId            nvarchar(...)
CreatedAt           datetime2
ImagePath           nvarchar(...)
ProcessingStatus    nvarchar(...)
OverallConfidence  decimal(...)
```

### `SkuScanField`

```text
Id                  bigint / identity
ScanId              GUID
FieldName           nvarchar(...)
RawValue            nvarchar(...)
NormalizedValue     nvarchar(...)
Confidence          decimal(...)
Source              nvarchar(...)
CreatedAt           datetime2
```

### `SkuScanOcr`

Store the OCR tokens/bounding boxes during R&D.

Suggested columns:

```text
Id                  bigint / identity
ScanId              GUID
Text                nvarchar(...)
X                   int
Y                   int
Width               int
Height              int
Confidence          decimal(...)
CreatedAt           datetime2
```

### Optional `SkuMaster`

If Reliance Retail provides product master data, support:

```text
Id
GTIN
SKU
ProductName
Manufacturer
```

This is useful for product context and future barcode-based matching.

Do not introduce a separate database for OCR metadata.

---

## 16. Persistence Rule

During R&D, preserve enough information to diagnose OCR failures.

For every scan, keep:

- scan ID
- captured image path/reference
- raw OCR results
- extracted values
- normalized values
- confidence values
- operator-confirmed values
- processing timestamps/status

This audit trail is important because field-level accuracy is the main R&D concern.

---

## 17. QR Code and Printer Flow

After the operator confirms the OCR result:

```text
Android
  |
  v
Confirm result
  |
  v
POST /confirm
  |
  v
Server persists data
  |
  v
Generate QR payload
  |
  v
Print using ZQ320-class printer
```

### QR physical size

The printed QR code must be **10 mm × 10 mm**.

### QR payload contents

The QR must carry the confirmed field data, not just an identifier:

- `scanId`
- `batchNumber`
- `manufacturingDate`
- `expiryDate`
- `lotCode`
- `mrp`
- any further fields extracted by OCR and confirmed by the operator

Use a compact delimited encoding so the payload stays inside the density budget below.

Example:

```text
SCAN-000001|A23C91|2026-07|2028-06|K18P2|245.00
```

Prefer uppercase alphanumeric content. ZPL QR alphanumeric mode packs two characters per 11 bits; mixed-case or symbol-heavy payloads fall back to byte mode and roughly halve the usable capacity.

### Density budget (do not exceed)

The ZQ320 print head is **203 dpi = 8 dots/mm**, so 10 mm = **80 dots**.

| Setting | Value |
| --- | --- |
| ZPL magnification (`^BQ` factor) | 2 → module = 2 dots = 0.25 mm |
| Modules available in 80 dots | 40 |
| Chosen QR version | 5 (37 × 37 modules = 74 dots = 9.25 mm) |
| Error correction level | M |
| Capacity at version 5 / EC M | ~154 alphanumeric chars, ~106 bytes |

The example payload above is 47 characters, so there is comfortable headroom for extra fields.

Rules:

- Do not drop magnification below 2. A 1-dot module is not reliably scannable on thermal media.
- Do not let the payload push the symbol past version 5 at magnification 2 — it will overflow the 10 mm box.
- The 4-module quiet zone sits **outside** the 10 mm symbol area. Reserve it in the label template.
- If a future field set exceeds capacity, fall back to `scanId` only and treat the backend as the lookup, rather than shrinking the module size.

The backend remains the source of truth regardless of QR contents.

The exact QR content can be changed later without changing the core OCR pipeline.

---

## 18. Printer Integration

The Android application is responsible for communicating with the Zebra mobile printer.

### Transport and SDK

- Printer: Zebra **ZQ320** class, 203 dpi, mobile thermal.
- Transport: **Bluetooth**.
- SDK: **Zebra ZSDK (Link-OS Multiplatform SDK for Android)**.
- Label language: **ZPL**.

The ZSDK is distributed as an AAR and is not available on Maven Central. Vendor it into the Android project's local libs directory and commit the dependency declaration, not the binary, if repository policy forbids binaries.

ZSDK is for printing only. Device gating uses EMDK (section 4); image capture uses CameraX. DataWedge is not used anywhere in this build.

### Abstraction

Hide printer-specific code behind a small abstraction such as:

```kotlin
interface LabelPrinter {
    suspend fun print(scan: ConfirmedScan): PrintResult
}
```

Only the ZSDK implementation may import Zebra SDK types. Connection handling, retries, and ZPL string building stay behind this interface.

### Print template

Keep the print template simple for the POC.

Suggested contents:

```text
RELIANCE RETAIL
Product/SKU
Batch
MFG
EXP
LOT
MRP
QR CODE (10 mm x 10 mm, see section 17)
```

Emit ZPL directly. The QR uses `^BQ` with magnification 2 and error correction M, per the density budget in section 17.

Do not tightly couple OCR logic to printer logic.

### Failure handling

- Printer not paired / not reachable → surface a clear operator error and allow retry. Never fail silently.
- A print failure must not roll back the confirmed scan. The data is already persisted; printing is a separate, retryable step.
- Log printer request failures per section 21.

---

## 19. Security / Transport

The Android app must communicate with the server over HTTPS in the intended deployment.

Do not hard-code credentials in source code.

Use configuration/environment variables for:

- SQL Server connection string
- server URLs
- printer configuration
- OCR configuration
- file storage paths
- authentication secrets, if required

For the POC, a simple authenticated API is acceptable. The authentication mechanism should be isolated from business logic.

---

## 20. Image Storage

Do not store large image binaries directly in SQL Server unless there is a specific requirement to do so.

Prefer:

```text
Windows filesystem
        |
        +-- scan images
        +-- processed images (optional)
        +-- debug artifacts (optional)
```

Store the image path/reference in SQL Server.

Use a deterministic folder structure such as:

```text
images/
  2026/
    08/
      14/
        SCAN-000001.jpg
```

The exact folder structure can vary.

---

## 21. Logging

The backend must provide useful structured logs.

At minimum log:

- Request/scan ID
- Processing start/end
- OCR duration
- preprocessing duration
- extraction duration
- overall processing duration
- extracted field names
- confidence values
- failure reasons
- database failures
- printer request failures

Do not log sensitive credentials.

Avoid dumping entire raw images or huge payloads into normal application logs.

---

## 22. Error Handling

The API must return clear errors.

Examples:

### Invalid image

```http
400 Bad Request
```

### OCR processing failure

```http
500 Internal Server Error
```

with a safe, user-friendly error body.

### Database failure

Return a server error and ensure the scan's processing state makes the failure diagnosable.

### No text detected

This is not necessarily a server error.

Return a valid scan result such as:

```json
{
  "status": "NO_TEXT_DETECTED"
}
```

The Android UI should ask the operator to recapture.

---

## 23. Performance Goals

This is CPU-only.

Do not optimize for theoretical maximum throughput before the POC works.

Measure:

- image upload time
- preprocessing time
- OCR time
- extraction time
- database time
- end-to-end latency

The first objective is stable and accurate extraction from real SKU images.

A simple sequential server is acceptable for the initial R&D implementation.

---

## 24. Accuracy Goals

Measure accuracy at the **field level**, not just generic OCR character accuracy.

### Two tiers, deliberately

The five fields are not equally recoverable, and holding them to one number hides the only figure that matters commercially.

**Core fields — MRP, manufacturing date, expiry date. Target 95%+.**

They have a fixed shape: a currency amount, a calendar date. A recogniser has structure to check itself against, a wrong reading is often self-evidently wrong, and normalization can repair formatting. They are also printed on essentially every pack, so they are the fields an operator would otherwise retype every time.

**Code fields — batch number, lot code. Best effort.**

Arbitrary alphanumeric strings with no format and no redundancy. One misread character fails the field, and nothing in the value can reveal the error. Operator entry is the expected path here, not a failure of the system.

Report the two tiers separately. A blended figure lets a good code-field result mask a bad MRP, and the reverse.

| Field | Tier | Metric |
| --- | --- | --- |
| MRP | Core | Numeric accuracy |
| MFG | Core | Date accuracy |
| EXP | Core | Date accuracy |
| Batch | Code | Exact/normalized match accuracy |
| LOT | Code | Exact/normalized match accuracy |
| Overall | — | Core and code reported separately, never blended |

Also track:

- False accepts
- False rejects
- No-detection rate
- Manual correction rate
- Average processing time

A wrong expiry date is more serious than a recapture request. Prefer safe review over false acceptance.

---

## 25. R&D Test Data

For the immediate POC, use a small collection of real representative Reliance Retail SKU-label images.

Do not build a model-training dataset as part of this scope.

The immediate objective is to test the OCR/extraction pipeline against realistic inputs.

Create a local test directory such as:

```text
sample_data/
├── good/
├── low_light/
├── blur/
├── glare/
├── curved_packaging/
└── rotated/
```

Store expected field values in a simple JSON/CSV file so that automated field-level accuracy tests can be run.

Example:

```json
{
  "image": "sample_data/good/sku_001.jpg",
  "expected": {
    "batchNumber": "A23C91",
    "manufacturingDate": "2026-07",
    "expiryDate": "2028-06",
    "lotCode": "K18P2",
    "mrp": "245.00"
  }
}
```

### Zero-shot evaluation

**Do not curate or download public datasets for this build.**

PaddleOCR is used zero-shot: no fine-tuning, no training data, no dataset engineering. Point the pipeline at real Reliance Retail label images and measure what it does out of the box.

Rationale: PP-OCRv5 is already a strong general recognizer. The project's variable is the **extraction and normalization logic layered on top of it**, not the OCR model itself. Building a dataset pipeline before knowing where PaddleOCR actually fails would be effort spent on the wrong layer.

Consequence, accepted: until real Reliance Retail images exist, there is no accuracy gate to run. Phase 2 logic is built and unit-tested against hand-written token fixtures; the accuracy number arrives with the images. The `sample_data/` bucket structure and ground-truth format stay in place, ready to be filled.

If zero-shot recognition turns out to be the bottleneck — rather than extraction — revisit this decision and record the pivot per section 1.

---

## 26. Development Priorities

Implement in this order:

### Priority 1

Get the backend running locally on Windows:

```text
FastAPI
  -> image upload
  -> PaddleOCR
  -> JSON response
```

### Priority 2

Add OpenCV preprocessing.

### Priority 3

Add field extraction and normalization.

### Priority 4

Add confidence calculation and validation.

### Priority 5

Add SQL Server persistence.

### Priority 6

Build the TC22 Android scan/confirmation UI.

### Priority 7

Add ZQ320-class printing.

### Priority 8

Add IIS reverse-proxy/deployment configuration.

Do not start with deployment complexity.

---

## 27. Definition of Done for the R&D POC

The POC is considered successful when all of the following work end-to-end:

1. TC22 opens the camera.
2. Operator captures a real SKU-label image.
3. Android uploads the image to the Windows server.
4. FastAPI receives the image.
5. OpenCV preprocesses it.
6. PaddleOCR extracts text and bounding boxes.
7. Backend identifies at least the target fields where present:
   - Batch number
   - Manufacturing date
   - Expiry date
   - Lot code
   - MRP
8. Backend validates and normalizes the values.
9. Android receives structured JSON.
10. Android displays the values for operator review/edit.
11. Operator confirms.
12. Backend stores the confirmed data in SQL Server.
13. Android/server prepares the QR payload.
14. ZQ320-class printer prints a label containing the required information and QR code.
15. Errors are handled cleanly.
16. Logs allow an engineer to diagnose failed scans.

---

## 28. Coding Guidelines for Claude Code

When implementing this project:

- Prefer simple, readable code over abstraction-heavy code.
- Keep modules small and single-purpose.
- Do not introduce infrastructure that is not required by the current scope.
- Keep OCR, extraction, validation, persistence, and printing separate.
- Use typed request/response models with Pydantic on the server.
- Use Kotlin data classes for Android API models.
- Make configuration externalized.
- Avoid hard-coded paths, credentials, URLs, and printer addresses.
- Add unit tests for date parsing and field extraction.
- Add automated OCR integration tests against representative sample images where possible.
- Preserve raw OCR output during R&D for debugging.
- Never silently fabricate missing field values.
- Prefer deterministic rules whenever a rule can solve the problem reliably.
- Add comments where OCR heuristics are non-obvious.
- Do not over-engineer concurrency, queues, caching, or distributed processing.

---

## 29. Suggested Repository Layout

A single repository is sufficient.

**The backend lives at the repository root. The Android project lives in `android/`.**

```text
project-root/                 <- git root, backend root
├── CLAUDE.md
├── PLAN.md
├── README.md
├── .gitignore
├── requirements.txt
├── .env.example
├── app/                      <- FastAPI application
│   ├── main.py
│   ├── config.py
│   ├── api/
│   ├── models/
│   ├── services/
│   ├── db/
│   └── utils/
├── tests/
├── scripts/                  <- local test client, model warmup
├── config/                   <- field alias tables, thresholds
├── models/                   <- vendored PaddleOCR model files
├── sample_data/
├── sql/
│   ├── schema.sql
│   └── seed.sql
├── deployment/
│   └── iis/
└── android/                  <- Android Gradle project root
    └── retail-ocr/
```

Keep Android and backend code clearly separated. Nothing under `android/` may import backend code, and nothing in `app/` may reference Android paths.

Runtime output directories (`images/`, `logs/`, Paddle model cache) must be git-ignored and configured by environment variable, never committed.

---

## 30. First Implementation Task

Start by implementing the backend POC before the full Android application.

The first runnable milestone must be:

```text
POST image
   -> FastAPI
   -> OpenCV preprocessing
   -> PaddleOCR
   -> field extraction
   -> validation
   -> JSON result
```

Provide a simple local test client or curl example so the OCR pipeline can be tested without the Android device.

Once the backend extraction is reliable on representative sample images, integrate the TC22 application.
