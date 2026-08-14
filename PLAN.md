# D-Mart SKU Label OCR — Phase Plan

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
| R3 | No real D-Mart label images yet | Phase 2 accuracy gate is unverifiable | Build against ExpDate (1,767 real product photos, date/prod/code annotated) + Kaggle sets — see [sample_data/README.md](sample_data/README.md). Real-image run is a **separate second gate** | 2 | Mitigated |
| R4 | Indian retail labels often carry Devanagari/regional script alongside English | Junk tokens, bad spatial association | Start `lang="en"`; evaluate multilingual only if real images demand it. Log unrecognized-script rate | 2 | Open |
| R5 | TC22 not yet connected | Camera/printer assumptions untested | Develop on SM-S711B; TC22 arriving. **EMDK/DataWedge ruled out** — CameraX only, so the app builds and runs on any Android device | 4 | Mitigated |
| R6 | ZQ320 needs Zebra ZSDK (Link-OS AAR, not on Maven Central) | Build blocked | Obtain SDK during Phase 3 so it is in hand before Phase 5. Vendor to `android/dmart-ocr/app/libs/` (git-ignored) | 5 | Open |
| R7 | Self-signed HTTPS cert rejected by TC22 | End-to-end blocked at the last step | Decide cert strategy before Phase 6; if self-signed, plan Android network-security-config + device trust store install | 6 | Open |
| R8 | CPU-only OCR latency. PP-OCRv5 published CPU times: mobile ~1.75 s/image, server ~4.34 s/image, before OpenCV preprocessing and multi-variant runs | Poor operator UX | Measure in Phase 1. Model combination is config-driven so it can be tuned, not rewritten. Only if p95 is bad, add the optional WebSocket (§14) — **no queue** | 1 → 7 | Quantified |
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

**Model decision (highest ROI): `PP-OCRv5_server_det` + `PP-OCRv5_mobile_rec`.**

| Variant | Det acc | Rec acc | CPU s/image |
| --- | --- | --- | --- |
| mobile + mobile | 0.770 | 0.8015 | ~1.75 |
| server + server | 0.827 | 0.8401 | ~4.34 |

Detection quality dominates on small dense label print; recognition targets are short alphanumeric fields where the cheaper model gives up little. Both names are config-driven so all four combinations are A/B testable in Phase 2 without a code change.

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

## Phase 1 — Backend OCR Spine (§26 P1 + P2)

**Goal:** image in → OCR tokens with bounding boxes out. This is the load-bearing phase; the rest is logic on top of it.

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

## Phase 2 — Extraction, Normalization, Validation, Confidence (§26 P3 + P4)

**Goal:** tokens → the five target fields, normalized, validated, scored. This is where POC success is actually won or lost.

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
- Unit tests for date parsing and field extraction (explicitly required by §28)

**Exit gate:** §30 first-runnable-milestone complete end-to-end on the server, plus a printed accuracy report over `sample_data/`. **Second gate, once real D-Mart images arrive (R3): re-run and tune thresholds.**

**Est.** 3–4 days

---

## Phase 3 — SQL Server Persistence (§26 P5)

**Goal:** durable audit trail sufficient to diagnose any failed scan (§16).

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

## Phase 4 — Android App (§26 P6)

**Goal:** the operator workflow of §4, running on real hardware.

**Deliverables** — `android/dmart-ocr/`
- Kotlin, Gradle wrapper, AGP 8.x + JDK 17, Compose. `targetSdk 35`, `minSdk` set to the TC22's actual API level (see Open Decisions Q1)
- Scan screen: CameraX preview + target-area overlay + capture (§4)
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
- ZQ320-class implementation via Zebra Link-OS SDK over Bluetooth; CPCL/ZPL template per §18 (D-MART / SKU / Batch / MFG / EXP / LOT / MRP / QR)
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
| Q1 | EMDK / DataWedge needed? | **No.** CameraX alone. TC22 arriving soon; app stays device-agnostic so development is never blocked on hardware. Written into CLAUDE.md §4. |
| Q2a | Repo layout | **Backend at repository root**, Android in `android/`. CLAUDE.md §5 and §29 updated to match. |
| Q2b | Model download on first run | **Accepted.** Progress state in app + `scripts/warmup_models.py` + startup warmup. Models pre-initialized before any demo. |
| Q4a | QR specification | **10 mm × 10 mm**, carries all confirmed OCR fields, ZPL `^BQ`, Zebra ZSDK over Bluetooth. CLAUDE.md §17 and §18 rewritten. |
| Q8 | PaddleOCR version | **Keep 3.4.1 / paddlepaddle 3.3.1.** See §1a. |

### Still open

Blocking phase noted. None block Phase 0–2.

| # | Question | Blocks |
| --- | --- | --- |
| Q2 | Real D-Mart SKU label images — how many, when? English-only or mixed script? (R3, R4) | P2 second gate |
| Q3 | Target SQL Server: keep local `SQLEXPRESS` for dev, or a designated instance + auth mode (SQL auth vs Windows auth)? | P3 |
| Q4 | Zebra ZSDK access, and is a ZQ320 available for testing? | P5 |
| Q5 | POC auth mechanism — static API key header is the simplest option satisfying §19. Confirm, or name the required scheme. | P1 |
| Q6 | HTTPS certificate — corporate CA or self-signed? Self-signed means installing trust on every TC22. | P6 |
| Q7 | Is D-Mart product master data (`SkuMaster`, §15) available, or is that table deferred? | P3 |

**Proposed defaults if unanswered:** Q3 → local `SQLEXPRESS` + Windows auth; Q5 → static API key header; Q7 → create the table, leave it empty.

---

## Pivot Policy

This is stage one. Approaches here are engineering defaults, not commitments.

When something changes — a better OCR configuration, a different extraction strategy, a scope change from D-Mart — **update `CLAUDE.md` and this file in the same change that alters the code.** Record why. A spec that drifts from the implementation is worse than no spec.

`CLAUDE.md` §2 (out of scope) and §24 (accuracy goals) are the two sections to challenge first if the POC stalls.
