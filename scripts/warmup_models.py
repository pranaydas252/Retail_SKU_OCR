"""Download and initialize PaddleOCR models ahead of time.

PaddleOCR fetches model files from Hugging Face on first use. Run this before
a demo or a deployment so no operator ever waits on a download.

    python scripts/warmup_models.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services.ocr_service import get_ocr_service  # noqa: E402
from app.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    print("Warming up OCR models")
    print(f"  detection:   {settings.ocr_det_model_name}")
    print(f"  recognition: {settings.ocr_rec_model_name}")
    print(f"  oneDNN:      {settings.ocr_enable_mkldnn}")
    print()

    started = time.perf_counter()
    try:
        get_ocr_service().warmup()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Ready in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
