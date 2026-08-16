"""Write out exactly what the vision model receives, for a human to look at.

Every accuracy argument about preprocessing is really an argument about what
the model can see, and that is invisible from the outside: by the time an
image reaches Ollama it has been cropped, reflowed, downscaled to the model's
budget and JPEG-compressed. Reasoning about the 2692px capture on disk is
reasoning about a different picture.

So these are not re-renders. Each file is the exact JPEG byte buffer that
would be base64-encoded and posted to the model, written to disk instead.
What you see is what it sees.

    python scripts/dump_vl_input.py
    python scripts/dump_vl_input.py --image images/SAMPLE_20260815_154330_158.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.field_extractor import load_field_specs  # noqa: E402
from app.services.ocr_service import OcrService  # noqa: E402
from scripts.crop_bakeoff import (  # noqa: E402
    crop_montage,
    crop_union,
    detect,
    reflow,
)

#: A deliberately cluttered capture. On this one the model returned only the
#: pack's printed labels and none of the values, so it is the case the
#: preprocessing exists to fix — a clean label would show nothing interesting.
DEFAULT_IMAGE = "images/SAMPLE_20260815_154304_075.jpg"

OUTPUT_DIR = Path("images/vl_input")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--side", type=int, default=960,
                        help="PP-OCRv5 detection side length")
    parser.add_argument("--max-side", type=int, default=1100,
                        help="longest side the vision model is given")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    source = Path(args.image)
    image = cv2.imread(str(source))
    if image is None:
        print(f"cannot read {source}", file=sys.stderr)
        return 1

    service = OcrService(get_settings().model_copy(
        update={"ocr_det_limit_side_len": args.side}
    ))

    aliases = set()
    for spec in load_field_specs():
        for alias in spec.aliases:
            cleaned = "".join(c for c in alias.upper() if c.isalnum())
            if len(cleaned) >= 3:
                aliases.add(cleaned)

    tokens, _ = detect(service, image, args.side)
    occupied = [(t.x, t.x + t.width) for t in tokens]
    montage = crop_montage(image, tokens, aliases)

    variants = {
        "1_full": image,
        "2_union": crop_union(image, tokens),
        "3_montage": montage,
        # Kept visible precisely because it is wrong: on a column layout it
        # splits labels away from their values. Seeing that is the point.
        "4_reflow_unsafe": reflow(montage, occupied),
    }

    args.out.mkdir(parents=True, exist_ok=True)
    for existing in args.out.glob("*.jpg"):
        existing.unlink()

    original_side = max(image.shape[:2])
    print(f"source {source.name}  {image.shape[1]}x{image.shape[0]}  "
          f"{len(tokens)} text regions detected\n")
    print(f"{'VARIANT':12s} {'BEFORE RESIZE':>16s} {'WHAT THE MODEL SEES':>21s} "
          f"{'MAGNIF':>7s} {'KB':>6s}")
    print("-" * 68)

    for name, variant in variants.items():
        if variant is None or variant.size == 0:
            variant = image

        # The same two steps ask_vl performs, in the same order: fit the
        # longest side to the model's budget, then JPEG at quality 92.
        sent = image_service.resize_for_ocr(variant, args.max_side)
        ok, buffer = cv2.imencode(".jpg", sent, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            continue

        # Text is magnified when the crop shrinks the longest side, because
        # the downscale to the budget is then gentler than the full frame's.
        magnification = original_side / max(variant.shape[:2])

        path = args.out / f"{name}_{sent.shape[1]}x{sent.shape[0]}.jpg"
        path.write_bytes(buffer.tobytes())

        print(f"{name:12s} {f'{variant.shape[1]}x{variant.shape[0]}':>16s} "
              f"{f'{sent.shape[1]}x{sent.shape[0]}':>21s} "
              f"{magnification:6.2f}x {len(buffer) / 1024:6.0f}")

    print(f"\nwritten to {args.out}/")
    print("These are the literal bytes posted to the model, not re-renders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
