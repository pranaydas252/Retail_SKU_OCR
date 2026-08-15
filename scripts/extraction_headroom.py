"""How much accuracy is left on the code side, as opposed to the engine side?

Every value the pipeline gets wrong falls into one of two classes, and they
have completely different fixes:

* PRESENT — the value is somewhere in the recognised text, but extraction did
  not pull it out. That is a rules bug, and it is fixable here, today, without
  changing the OCR engine.

* ABSENT — the value never reached the text at all. No amount of rule work
  recovers it. That is the recognition ceiling and only a better engine moves
  it.

Reporting a single accuracy number hides this split, and the split is the
whole decision: it says whether to spend effort on the extractor or on the
model.

Matching is deliberately generous. A value counts as PRESENT if its
alphanumeric skeleton appears anywhere in the concatenated text, so
"Rs.319.00" contains "319.00" and a date printed "03/06/2026" contains its own
digits. Generosity is correct here — it inflates the fixable class, which
makes the ceiling claim conservative rather than self-serving.

    python scripts/extraction_headroom.py
    python scripts/extraction_headroom.py --side 1920 --rec en_PP-OCRv5_mobile_rec
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# The multilingual recogniser returns CJK glyphs on dot-matrix print, and a
# Windows console is cp1252, which cannot encode them - printing the raw text
# crashes the run. Replace rather than fail: the point of --show is to see
# what came back, and a "?" for an unprintable glyph still shows that
# something non-ASCII was recognised.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import OcrService  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")


def skeleton(value) -> str:
    """Alphanumeric characters only, uppercased.

    Strips the punctuation and currency marks that differ between how a value
    is printed and how the pipeline expresses it, so the comparison is about
    the characters that were actually recognised.
    """
    return "".join(c for c in str(value).upper() if c.isalnum())


def date_forms(value: str) -> list[str]:
    """The ways an ISO date could legitimately appear in printed text.

    Ground truth is normalized ("2026-06-03") but a pack prints "03/06/2026"
    or "03 JUN 26". Searching only for the ISO skeleton would report a
    correctly recognised date as ABSENT and blame the engine for an
    extraction-side formatting difference.
    """
    parts = value.split("-")
    if len(parts) == 3:
        y, m, d = parts
        return [f"{d}{m}{y}", f"{d}{m}{y[2:]}", f"{y}{m}{d}", f"{m}{d}{y}"]
    if len(parts) == 2:
        y, m = parts
        return [f"{m}{y}", f"{m}{y[2:]}", f"{y}{m}"]
    return [skeleton(value)]


def candidates(field: str, want: str) -> list[str]:
    if field.endswith("Date"):
        return date_forms(want)
    if field == "mrp":
        # "245.00" is often printed "245" or "245/-"
        whole = want.split(".")[0]
        return [skeleton(want), skeleton(whole)]
    return [skeleton(want)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", type=int, default=1920)
    parser.add_argument("--rec", default="PP-OCRv5_mobile_rec")
    parser.add_argument("--show", action="store_true",
                        help="print the recognised text for every image")
    args = parser.parse_args()

    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    service = OcrService(get_settings().model_copy(update={
        "ocr_rec_model_name": args.rec,
        "ocr_det_limit_side_len": args.side,
    }))

    verdicts: Counter[str] = Counter()
    fixable: list[str] = []
    unreachable: list[str] = []

    for entry in truth["images"]:
        image = cv2.imread(entry["image"])
        if image is None:
            continue

        prepared = image_service.build_variant(image, "original")
        prepared = image_service.resize_for_ocr(prepared, args.side)
        tokens = service.recognize(prepared)

        # One flat string, because the question is only whether the characters
        # were recognised at all - not whether they landed in one token.
        blob = skeleton(" ".join(t.text for t in tokens))
        got = {
            f: c.normalized.value
            for f, c in extract_fields(tokens).items()
            if c.normalized.ok
        }

        name = Path(entry["image"]).name
        if args.show:
            print(f"\n=== {name}")
            print("    " + " | ".join(t.text for t in tokens)[:400])

        for field, want in entry["expected"].items():
            if want is None:
                continue

            extracted = got.get(field)
            correct = extracted is not None and skeleton(extracted) == skeleton(want)
            present = any(c and c in blob for c in candidates(field, want))

            if correct:
                verdicts["correct"] += 1
            elif present:
                verdicts["fixable"] += 1
                fixable.append(f"{name:34s} {field:18s} want {want!r} got {extracted!r}")
            else:
                verdicts["unreachable"] += 1
                unreachable.append(f"{name:34s} {field:18s} want {want!r}")

    total = sum(verdicts.values())
    print(f"\n{args.rec} @ {args.side}px, {total} required values\n")
    print(f"  correct              {verdicts['correct']:3d}  {verdicts['correct'] / total:5.0%}")
    print(f"  in text, not pulled  {verdicts['fixable']:3d}  {verdicts['fixable'] / total:5.0%}   <- fixable in code")
    print(f"  never recognised     {verdicts['unreachable']:3d}  {verdicts['unreachable'] / total:5.0%}   <- engine ceiling")

    reachable = verdicts["correct"] + verdicts["fixable"]
    print(f"\n  ceiling if extraction were perfect: {reachable / total:.0%}")

    if fixable:
        print(f"\nFIXABLE ({len(fixable)}) - value was recognised, extraction missed it:")
        for line in fixable:
            print(f"  {line}")

    if unreachable:
        print(f"\nUNREACHABLE ({len(unreachable)}) - value never appeared in the text:")
        for line in unreachable:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
