# Retail SKU OCR

Server-backed SKU/product label OCR for Reliance Retail.

A Zebra TC22 captures a product label, a Windows-hosted FastAPI service extracts the printed fields with PaddleOCR on CPU, the operator confirms or corrects the values, the result is stored in SQL Server, and a Zebra ZQ320 prints a label carrying a 10 mm × 10 mm QR code.

This is an R&D proof of concept. Field-level extraction accuracy is the objective, not throughput.

---

## Repository layout

The **backend lives at the repository root**. The Android project lives in `android/`.

```text
.
├── CLAUDE.md              # Authoritative project specification
├── PLAN.md                # Phase-wise execution plan
├── requirements.txt
├── .env.example
├── app/                   # FastAPI application
├── tests/
├── scripts/               # Local test client, model warmup
├── config/                # Field alias tables, thresholds
├── models/                # Vendored PaddleOCR models (git-ignored)
├── sample_data/           # Test images + ground truth
├── sql/                   # Schema and seed
├── deployment/iis/        # Windows Server deployment
└── android/retail-ocr/     # Kotlin Android app
```

---

## Stack

| Layer | Choice |
| --- | --- |
| Device | Zebra TC22 **only** — EMDK device gate; CameraX for capture; no DataWedge |
| App | Kotlin + XML views, Material 3, light theme only — **no Compose** |
| Capture gate | ML Kit text recognition (bundled) on the preview stream — scores framing, skew and text presence at ~250 ms, and deskews the crop. Not used to extract fields |
| Transport | HTTPS REST, multipart image upload |
| Server | Python 3.12, FastAPI, Uvicorn behind IIS |
| Preprocessing | OpenCV |
| OCR | PaddleOCR 3.4.1 / PaddlePaddle 3.3.1, PP-OCRv5, **CPU only** |
| Second engine | `qwen3-vl:4b-instruct` via Ollama, local **CPU only**. Same extraction path, so its output is directly comparable |
| Extraction | Deterministic rules, aliases, spatial association |
| Database | Microsoft SQL Server (ODBC Driver 18) |
| Printer | Zebra ZQ320 over Bluetooth, ZSDK (Link-OS), ZPL |

Deliberately excluded: Redis, RabbitMQ, Celery, Kafka, PostgreSQL, vector databases, cloud LLM APIs, GPU inference, agent frameworks, Kubernetes, microservices, and any model training or fine-tuning. See `CLAUDE.md` section 2.

A vision-language model is the one exception, added 2026-08-16 after PP-OCRv5 was measured unable to read inkjet date stamps. It runs locally on CPU through Ollama — no cloud, no GPU, no cost — so the constraints above hold.

---

## Backend setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# edit .env — at minimum SQL_CONNECTION_STRING and API_KEY
```

Create the database (SQL Server must be running):

```powershell
sqlcmd -S localhost\SQLEXPRESS -E -i sql\schema.sql
```

Warm the OCR models before first use. PaddleOCR downloads them on first run; do this ahead of any demo:

```powershell
python scripts\warmup_models.py
```

Run the server:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check:

```powershell
curl http://localhost:8000/api/v1/health
```

Test the OCR pipeline without a device. `--make-sample` renders a synthetic label so the wiring can be checked before real images exist:

```powershell
python scripts\test_client.py --make-sample
python scripts\test_client.py sample_data\good\synthetic_label.jpg
```

Or with curl:

```powershell
curl -F "image=@sample_data/good/synthetic_label.jpg" http://localhost:8000/api/v1/scans
```

Confirm a scan and read it back:

```powershell
curl -X POST -H "Content-Type: application/json" `
  -d '{\"fields\":{\"batchNumber\":\"A23C91\",\"mrp\":\"245.00\"}}' `
  http://localhost:8000/api/v1/scans/SCAN-000001/confirm

curl http://localhost:8000/api/v1/scans/SCAN-000001
```

Tests:

```powershell
python -m pytest tests -q
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Liveness and OCR readiness. Unauthenticated so IIS can probe it. |
| `POST` | `/api/v1/scans` | Upload a label image; returns extracted fields, tokens, and timings. |
| `POST` | `/api/v1/scans/{scanId}/confirm` | Persist operator-confirmed values; returns the QR payload. |
| `POST` | `/api/v1/scans/{scanId}/print` | Record that a label was printed; returns the QR payload. Called *after* the printer reports success. |
| `GET` | `/api/v1/scans/{scanId}` | Read a stored scan back, including operator edits. |

## Status

| Phase | State |
| --- | --- |
| 0 — Repo skeleton, config contract | Done |
| 1 — OCR spine: upload → OpenCV → PaddleOCR → tokens | Done |
| 2 — Field extraction, normalization, validation, confidence | Done; thresholds calibrated against the gated corpus |
| 3 — SQL Server persistence, confirm/fetch endpoints | Done |
| 4 — Android app (Kotlin + XML) | Done — built and verified on a real TC22 |
| 5 — ZQ320 printing | Done — confirmed end to end on a physical printer, QR included |
| 6 — IIS deployment | Not started |

The full chain runs on real hardware: capture → OCR → confirm → SQL Server → print.

### Accuracy

Measured on the 19 ground-truthed ROI captures in `sample_data/roi/`, reported in the two tiers of `CLAUDE.md` section 24 — never blended.

| Tier | Fields | PP-OCRv5 alone | Both engines (ships) |
| --- | --- | --- | --- |
| Core | MRP, manufacturing date, expiry date | 55% | **74%** |
| Codes | Batch number, lot code | 47% | **71%** |

The second engine is a Qwen vision-language model run locally on CPU through Ollama. It costs latency, not money: no cloud, no GPU. It closes most of the gap by answering where PP-OCRv5 declines — 3 missed core values against 19.

Core is still short of the 95% target. Recognition, not extraction, is the remaining ceiling — see `PLAN.md`.

### What the confidence bands mean

HIGH means **both engines read the value and read it the same**. That is a rule, not a score threshold, and it is what makes the band worth reading:

| Band | Values | Correct | Wrong |
| --- | --- | --- | --- |
| HIGH | 31 | 31 | **0** |
| REVIEW | 15 | 10 | 5 |
| LOW | 22 | 10 | 12 |

Agreement predicts correctness far better than the score does: the two highest-scoring *wrong* values in the corpus score 0.869 and 0.783, above any threshold worth setting, and both are uncorroborated. No wrong value reaches HIGH in either engine mode.

### Latency

Latency scales with the **number of detected text regions**, not image size: 4.6 s per ROI crop on the reference machine, and a full pack shot is much worse. This is why the app crops to the ROI window before uploading.

Running both engines costs far more than that — 95–160 s per scan on the reference laptop, essentially all of it the VLM on CPU. Set `VLM_TRIGGER=fallback` to trade the accuracy above for speed; the single-engine confidence threshold is separate and stays strict, so the HIGH band remains clean in that mode.

---

## Android setup

```text
android/retail-ocr/
```

Open in Android Studio. Requires JDK 17. Gradle wrapper is committed — no system Gradle needed.

The Zebra ZSDK (Link-OS) AAR is not on Maven Central. Place it in `android/retail-ocr/app/libs/` — it is git-ignored.

Set the backend base URL and API key in `local.properties` or via build config. Never hard-code them.

### Connecting the TC22 to the backend

Three things have to line up, and the failure looks identical for all three — the app reports a network error and says nothing about which one:

1. **Bind to all interfaces.** `--host 0.0.0.0`, as above. The default binds loopback only, which no device can reach.

2. **Use the LAN address, never `localhost`.** On the TC22, `localhost` is the TC22. Find the server's address and put it in the app's settings dialog, or in `local.properties` as the build default:

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 |
     Where-Object { $_.InterfaceAlias -notmatch 'Loopback' } |
     Select-Object InterfaceAlias, IPAddress
   ```

   Pick the Wi-Fi adapter's address — the TC22 is on Wi-Fi, so a wired or virtual adapter's address will not route. It is DHCP-assigned and moves; re-check it after a network change.

3. **Open the port inbound.** Windows Firewall blocks 8000 by default. From an **elevated** prompt:

   ```powershell
   New-NetFirewallRule -DisplayName "Reliance Retail OCR backend" `
     -Direction Inbound -Protocol TCP -LocalPort 8000 `
     -Action Allow -Profile Private
   ```

   `-Profile Private` only. Do not open this on a public network.

   Curling the server's own LAN address from the server does **not** test this — Windows treats that as local traffic and skips the inbound rule entirely. The only real check is from the device.

Debug builds allow cleartext HTTP so a laptop-hosted backend works before HTTPS exists. Release builds do not — see `CLAUDE.md` section 19.

---

## Verified environment

Confirmed working on the development machine as of 2026-08-14:

- Python 3.12.10, JDK 17.0.5
- SQL Server Express (`MSSQL$SQLEXPRESS`), ODBC Driver 18 and 17
- Android SDK platforms to android-37, build-tools to 36.1.0

---

## Documents

- **[CLAUDE.md](CLAUDE.md)** — the specification. It is the authority. It is also a living document; pivots are expected and should be written back into it.
- **[PLAN.md](PLAN.md)** — phases, gates, risk register, open decisions.
