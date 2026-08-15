"""Does resolution, or the recognition model, explain PP-OCRv5's failures?

Two variables that every earlier measurement held fixed:

* Detection side length. The ROI crop arrives at 2692px and the pipeline
  resizes it to 960 before OCR. A dot-matrix character 30px tall becomes 11px,
  against the roughly 32-48px that recognition wants. All 300 passes of the
  preprocessing bake-off ran at 960, so this was never varied.

* Recognition model. `PP-OCRv5_mobile_rec` is multilingual and has been
  returning CJK glyphs for dot-matrix on metal - "下下A" for a can base printed
  in ASCII. `en_PP-OCRv5_mobile_rec` cannot emit CJK at all.

    python scripts/resolution_sweep.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import OcrService  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")
CODE_FIELDS = ("batchNumber", "lotCode")

SIDE_LENGTHS = (960, 1440, 1920, 2560)
REC_MODELS = ("PP-OCRv5_mobile_rec", "en_PP-OCRv5_mobile_rec")


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def main() -> int:
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    base = get_settings()

    print(f"{'REC MODEL':26s} {'SIDE':>5s} {'CORE':>10s} {'CODES':>10s} "
          f"{'tokens':>7s} {'sec/img':>8s}")
    print("-" * 72)

    for rec in REC_MODELS:
        for side in SIDE_LENGTHS:
            service = OcrService(base.model_copy(update={
                "ocr_rec_model_name": rec,
                "ocr_det_limit_side_len": side,
            }))

            counts = {"core": Counter(), "code": Counter()}
            tokens = 0
            elapsed = 0.0

            for entry in truth["images"]:
                image = cv2.imread(entry["image"])
                if image is None:
                    continue

                prepared = image_service.build_variant(image, "original")
                prepared = image_service.resize_for_ocr(prepared, side)

                started = time.perf_counter()
                found = service.recognize(prepared)
                elapsed += time.perf_counter() - started
                tokens += len(found)

                got = {
                    f: c.normalized.value
                    for f, c in extract_fields(found).items()
                    if c.normalized.ok
                }

                for field, want in entry["expected"].items():
                    if want is None:
                        continue
                    tier = "core" if field in CORE_FIELDS else "code"
                    actual = got.get(field)
                    if actual is None:
                        counts[tier]["missed"] += 1
                    elif key(actual) == key(want):
                        counts[tier]["correct"] += 1
                    else:
                        counts[tier]["wrong"] += 1

            def pct(tier: str) -> str:
                c = counts[tier]
                total = c["correct"] + c["wrong"] + c["missed"]
                return f"{c['correct']}/{total} {c['correct'] / total:.0%}" if total else "-"

            n = len(truth["images"])
            print(
                f"{rec:26s} {side:5d} {pct('core'):>10s} {pct('code'):>10s} "
                f"{tokens:7d} {elapsed / n:8.1f}",
                flush=True,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
