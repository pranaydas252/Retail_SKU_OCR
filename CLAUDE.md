# CLAUDE.md — D-Mart SKU Label OCR Application

## 1. Project Overview

Build a server-backed Android application for D-Mart that captures images of SKU/product labels using a Zebra TC22, sends the image to a Windows server, extracts printed label information using CPU-based OCR, returns structured data to the Android app for operator confirmation, stores the confirmed data in Microsoft SQL Server, and prints a QR code/label using a Zebra ZQ320-class mobile printer.

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
- Vision-language models
- GPU inference
- AI agent frameworks
- Machine-learning training pipelines
- Kubernetes
- Microservice decomposition
- Cloud-specific infrastructure
- Complex distributed processing

Keep the implementation small, understandable, and suitable for a fast R&D POC.

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

#### Server-side enforcement

The client gate is **user experience, not security**. `Build.MANUFACTURER` is spoofable on a rooted device.

The real restriction is server-side: the Android app sends its device model and serial with every scan request, and the backend rejects any device that is not on the allowed Zebra device list. Treat the client check as a fast, friendly failure and the server check as the actual control.

### Camera workflow

The app should provide a simple scan screen:

1. Show camera preview.
2. Guide the operator to place the SKU label inside the target area.
3. Capture an image.
4. Perform only lightweight client-side image-quality checks where practical.
5. Upload the image to the backend.
6. Show processing state.
7. Receive extracted fields.
8. Present the extracted values in a confirmation/edit screen.
9. On operator confirmation, persist the data through the backend.
10. Trigger printing.

### Client-side image quality checks

Keep these lightweight. Do not build an AI vision pipeline on the TC22.

Useful checks:

- Image exists and is readable.
- Minimum resolution.
- Basic blur detection if practical.
- Basic brightness/darkness check if practical.
- Image is not obviously empty/invalid.

Do not over-engineer this for the first POC.

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

These are starting rules only. Keep them configurable and easy to extend.

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

If D-Mart provides product master data, support:

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
D-MART
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

Track at least:

| Field | Metric |
| --- | --- |
| Batch | Exact/normalized match accuracy |
| MFG | Date accuracy |
| EXP | Date accuracy |
| LOT | Exact/normalized match accuracy |
| MRP | Numeric accuracy |
| Overall | All required fields correct |

Also track:

- False accepts
- False rejects
- No-detection rate
- Manual correction rate
- Average processing time

A wrong expiry date is more serious than a recapture request. Prefer safe review over false acceptance.

---

## 25. R&D Test Data

For the immediate POC, use a small collection of real representative D-Mart SKU-label images.

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

### Public datasets

Public datasets are scaffolding used until real D-Mart label images exist. They are **not** a substitute for the accuracy gate.

Combine them at the **harness** level, not on disk. Each source keeps its own directory and its own licence; a shared adapter converts each into the ground-truth schema above, and the accuracy report breaks results down by source.

Do not merge sources into one undifferentiated image pool. They sit at different pipeline layers:

| Layer | Source | Tests |
| --- | --- | --- |
| End-to-end | ExpDate real product photos | Detection, recognition, spatial association, normalization |
| Unit | Cropped digit datasets | Recognizer and normalizer only, including `O/0 I/1 S/5 B/8 G/6 Z/2` disambiguation |

Rules:

- **Real images only in the accuracy gate.** Synthetic images may be used for stress-testing the normalizer, but must never be mixed into the reported accuracy number — synthetic data inflates it.
- **Report per source and per bucket.** A single blended figure hides which conditions fail.
- **Do not redistribute.** Several public sets state no licence, which grants no rights. Local R&D evaluation only. Downloaded data stays git-ignored.

### Known coverage gaps

Public data does not cover the full field set. This is why real D-Mart images remain mandatory:

| Field | Public coverage |
| --- | --- |
| `expiryDate` | Good |
| `manufacturingDate` | Good |
| `batchNumber` | Partial — usually annotated as a generic code, not distinguished from lot |
| `lotCode` | Partial — same problem |
| `mrp` | **None.** No public dataset covers Indian MRP printing |

There is also a domain gap: public expiry datasets are dominated by inkjet and dot-matrix date stamps on packaging, whereas D-Mart SKU labels are printed label stock. The failure modes differ. Treat public results as a smoke test of the logic, not a prediction of field performance.

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
    └── dmart-ocr/
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
