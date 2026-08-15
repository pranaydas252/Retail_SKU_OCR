"""Compare PP-OCRv5 detection models against real captured labels.

The mobile/server choice was made from published benchmarks and then measured
only on clean synthetic text, where server detection had nothing to prove. Real
D-Mart captures are dot-matrix on curved metal, rotated, with glare — the case
the server model is supposed to be for.

    python scripts/compare_models.py images/2026/08/15
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import OcrService  # noqa: E402
from app.config import get_settings  # noqa: E402

FIELDS = ["batchNumber", "manufacturingDate", "expiryDate", "lotCode", "mrp"]


def build(det: str, rec: str) -> OcrService:
    settings = get_settings().model_copy(
        update={"ocr_det_model_name": det, "ocr_rec_model_name": rec}
    )
    return OcrService(settings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    images = sorted(Path(args.directory).glob("*.jpg"))[: args.limit]
    if not images:
        print("no images found", file=sys.stderr)
        return 1

    combos = [
        ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"),
        ("PP-OCRv5_server_det", "PP-OCRv5_mobile_rec"),
        ("PP-OCRv5_server_det", "PP-OCRv5_server_rec"),
    ]

    for det, rec in combos:
        print(f"\n{'=' * 78}\n{det} + {rec}\n{'=' * 78}")
        service = build(det, rec)
        total_time = 0.0
        total_fields = 0

        for path in images:
            image = cv2.imread(str(path))
            if image is None:
                continue

            started = time.perf_counter()
            tokens = service.recognize(image)
            elapsed = time.perf_counter() - started
            total_time += elapsed

            fields = extract_fields(tokens)
            found = {k: v.normalized.value for k, v in fields.items() if v.normalized.ok}
            total_fields += len(found)

            summary = "  ".join(f"{k}={v}" for k, v in sorted(found.items())) or "-"
            print(f"{path.name:24s} {elapsed:6.2f}s {len(tokens):3d}tok  {summary}")

        print(
            f"{'TOTAL':24s} {total_time:6.2f}s "
            f"({total_time / len(images):.2f}s/img)  {total_fields} fields extracted"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
