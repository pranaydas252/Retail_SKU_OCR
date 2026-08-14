# Retail SKU OCR

Server-backed SKU/product label OCR for D-Mart.

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
└── android/dmart-ocr/     # Kotlin Android app
```

---

## Stack

| Layer | Choice |
| --- | --- |
| Device | Zebra TC22 **only** — EMDK device gate; CameraX for capture; no DataWedge |
| App | Kotlin, Jetpack Compose |
| Transport | HTTPS REST, multipart image upload |
| Server | Python 3.12, FastAPI, Uvicorn behind IIS |
| Preprocessing | OpenCV |
| OCR | PaddleOCR 3.4.1 / PaddlePaddle 3.3.1, PP-OCRv5, **CPU only** |
| Extraction | Deterministic rules, aliases, spatial association |
| Database | Microsoft SQL Server (ODBC Driver 18) |
| Printer | Zebra ZQ320 over Bluetooth, ZSDK (Link-OS), ZPL |

Deliberately excluded: Redis, RabbitMQ, Celery, Kafka, PostgreSQL, vector databases, LLMs, VLMs, GPU inference, agent frameworks, Kubernetes, microservices. See `CLAUDE.md` section 2.

---

## Backend setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# edit .env — at minimum SQL_CONNECTION_STRING and API_KEY
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

Test the OCR pipeline without a device:

```powershell
curl -F "image=@sample_data/good/sku_001.jpg" http://localhost:8000/api/v1/scans
```

---

## Android setup

```text
android/dmart-ocr/
```

Open in Android Studio. Requires JDK 17. Gradle wrapper is committed — no system Gradle needed.

The Zebra ZSDK (Link-OS) AAR is not on Maven Central. Place it in `android/dmart-ocr/app/libs/` — it is git-ignored.

Set the backend base URL and API key in `local.properties` or via build config. Never hard-code them.

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
