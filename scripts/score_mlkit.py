"""Score ML Kit's on-device output with the server's own extraction rules.

The Android side does recognition only and writes raw text plus bounding
boxes. Everything after that - field association, normalization, tiering -
runs here, through the same code path PP-OCRv5 and PaddleOCR-VL are judged by,
against the same ground truth. That is the whole point: a number produced by a
different harness would not be comparable to the 43% and 55% already on the
board, and the comparison is the reason for the experiment.

    adb shell mkdir -p /sdcard/Android/data/com.markss.dmartocr/files/bench_in
    adb push images/SAMPLE_*.jpg /sdcard/Android/data/com.markss.dmartocr/files/bench_in/
    (cd android/dmart-ocr && ./gradlew connectedDebugAndroidTest)
    adb pull /sdcard/Android/data/com.markss.dmartocr/files/mlkit_bench.json
    python scripts/score_mlkit.py mlkit_bench.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import OcrToken  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")
CODE_FIELDS = ("batchNumber", "lotCode")


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bench", type=Path, help="mlkit_bench.json pulled from the device")
    parser.add_argument("--truth", type=Path, default=TRUTH)
    parser.add_argument("--show", action="store_true",
                        help="print the recognised text for every capture")
    args = parser.parse_args()

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    expected = {Path(e["image"]).name: e["expected"] for e in truth["images"]}
    rows = json.loads(args.bench.read_text(encoding="utf-8"))

    per_field: dict[str, Counter] = {}
    false_accepts: list[str] = []
    total_ms = 0.0
    scored_images = 0

    for row in rows:
        name = row["image"]
        want_all = expected.get(name)
        if want_all is None:
            print(f"  {name}: no ground truth, skipped", file=sys.stderr)
            continue

        # ML Kit confidence is not read: it is not exposed consistently across
        # the result hierarchy, and the extractor treats 1.0 as "no signal"
        # rather than "certain" - application confidence is computed from
        # label match and spatial quality regardless (CLAUDE.md section 12).
        tokens = [
            OcrToken(
                text=t["text"], x=t["x"], y=t["y"],
                width=t["width"], height=t["height"], confidence=1.0,
            )
            for t in row["tokens"]
        ]

        total_ms += row.get("ms", 0.0)
        scored_images += 1

        got = {
            field: c.normalized.value
            for field, c in extract_fields(tokens).items()
            if c.normalized.ok
        }

        if args.show:
            print(f"\n=== {name}  {row.get('ms', 0):.0f}ms  {len(tokens)} lines")
            print("   " + " | ".join(t.text for t in tokens)[:400])
            print(f"   GOT : {got}")

        outcomes = {}
        for field, want in want_all.items():
            actual = got.get(field)
            if want is None:
                if actual is None:
                    continue
                outcome = "false accept"
                false_accepts.append(f"{name}: {field} = {actual!r}")
            elif actual is None:
                outcome = "missed"
            else:
                outcome = "correct" if key(actual) == key(want) else "wrong"
            outcomes[field] = outcome
            per_field.setdefault(field, Counter())[outcome] += 1

        if not args.show:
            print(f"{name:34s} {row.get('ms', 0):6.0f}ms  "
                  + "  ".join(f"{f}:{o[0].upper()}" for f, o in sorted(outcomes.items())))

    print()
    print(f"{'FIELD':22s} {'correct':>8s} {'wrong':>7s} {'missed':>7s} {'false+':>7s} {'acc':>7s}")
    print("-" * 62)

    scored = Counter()
    for field in sorted(per_field):
        counts = per_field[field]
        scored.update(counts)
        attempted = counts["correct"] + counts["wrong"] + counts["missed"]
        accuracy = counts["correct"] / attempted if attempted else 0.0
        print(f"{field:22s} {counts['correct']:8d} {counts['wrong']:7d} "
              f"{counts['missed']:7d} {counts['false accept']:7d} {accuracy:6.0%}")

    attempted = scored["correct"] + scored["wrong"] + scored["missed"]
    print("-" * 62)
    print(f"{'OVERALL':22s} {scored['correct']:8d} {scored['wrong']:7d} "
          f"{scored['missed']:7d} {scored['false accept']:7d} "
          f"{scored['correct'] / attempted if attempted else 0:6.0%}")

    print()
    for label, fields in (("CORE (mrp, mfg, exp)", CORE_FIELDS),
                          ("CODES (batch, lot)", CODE_FIELDS)):
        tier = Counter()
        for name in fields:
            tier.update(per_field.get(name, Counter()))
        tried = tier["correct"] + tier["wrong"] + tier["missed"]
        if not tried:
            continue
        print(f"{label:24s} {tier['correct']:3d} correct  {tier['wrong']:3d} wrong  "
              f"{tier['missed']:3d} missed  {tier['false accept']:3d} false+  "
              f"-> {tier['correct'] / tried:.0%}")

    if scored_images:
        print(f"\nML Kit on device: {scored_images} images, "
              f"{total_ms / scored_images:.0f}ms/image")

    if false_accepts:
        print(f"\nFALSE ACCEPTS ({len(false_accepts)}):")
        for line in false_accepts:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
