"""Field-level accuracy against real labels (CLAUDE.md section 24).

Measures what the POC exists to measure: whether the right value lands in the
right field, not character accuracy. Run it after any change to extraction,
normalization or OCR configuration.

    python scripts/measure_accuracy.py
    python scripts/measure_accuracy.py --json baseline.json

Outcomes per field, following section 24:

    correct       expected a value, produced exactly that value
    wrong         expected a value, produced a different one
    missed        expected a value, produced nothing
    false accept  expected NOTHING, produced a value

False accepts are reported separately and loudly. A wrong expiry date is more
serious than a recapture request, so a confidently wrong field is worse than an
empty one.
"""

from __future__ import annotations

import argparse
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
from app.services.ocr_service import get_ocr_service  # noqa: E402

DEFAULT_GROUND_TRUTH = "sample_data/roi_labels.json"

CORRECT, WRONG, MISSED, FALSE_ACCEPT = "correct", "wrong", "missed", "false accept"

#: Two accuracy tiers, because the fields are not equally recoverable.
#:
#: MRP, manufacturing date and expiry date have a fixed shape - a currency
#: amount, a calendar date - so a recogniser has structure to check itself
#: against and a wrong reading is often self-evidently wrong. They are also
#: printed on essentially every pack. These carry the real target.
#:
#: Batch and lot codes are arbitrary alphanumeric strings with no format and no
#: redundancy: one misread character fails the field and nothing in the value
#: can reveal it. They are best-effort, with operator entry the expected path
#: rather than a failure.
CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")
CODE_FIELDS = ("batchNumber", "lotCode")


def tier_of(field: str) -> str:
    return "core" if field in CORE_FIELDS else "code"



def compare(expected: str | None, actual: str | None) -> str | None:
    """Classify one field. None means the field was not scored."""
    if expected is None:
        return None if actual is None else FALSE_ACCEPT
    if actual is None:
        return MISSED
    return CORRECT if _same(expected, actual) else WRONG


def _same(expected: str, actual: str) -> bool:
    """Compare ignoring case and separators the normalizer may drop."""
    def key(v: str) -> str:
        return "".join(ch for ch in v.upper() if ch.isalnum())

    return key(expected) == key(actual)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write the raw per-field result here")
    parser.add_argument(
        "--truth", default=DEFAULT_GROUND_TRUTH,
        help="ground-truth file (default: ROI captures, the app's real input)",
    )
    args = parser.parse_args()

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    ocr = get_ocr_service()
    settings = get_settings()

    per_field: dict[str, Counter] = {}
    rows: list[dict] = []
    false_accepts: list[str] = []
    total_time = 0.0

    for entry in truth["images"]:
        path = Path(entry["image"])
        image = cv2.imread(str(path))
        if image is None:
            print(f"skipped (unreadable): {path}", file=sys.stderr)
            continue

        # Mirror scan_service exactly. Measuring raw full-resolution input
        # measures something the server never sees: it applies the configured
        # preprocessing variant and resizes to the detection side length first,
        # and that resize alone decides whether some stamps are found at all.
        started = time.perf_counter()
        prepared = image_service.build_variant(image, settings.variant_list[0])
        prepared = image_service.resize_for_ocr(prepared, settings.ocr_det_limit_side_len)
        tokens = ocr.recognize(prepared)
        elapsed = time.perf_counter() - started
        total_time += elapsed

        extracted = extract_fields(tokens)
        actual = {
            name: candidate.normalized.value
            for name, candidate in extracted.items()
            if candidate.normalized.ok
        }

        outcomes: dict[str, str] = {}
        for field, want in entry["expected"].items():
            got = actual.get(field)
            outcome = compare(want, got)
            if outcome is None:
                continue
            outcomes[field] = outcome
            per_field.setdefault(field, Counter())[outcome] += 1
            if outcome == FALSE_ACCEPT:
                false_accepts.append(f"{path.name}: {field} = {got!r} (pack has none)")

        rows.append({"image": path.name, "seconds": elapsed, "outcomes": outcomes,
                     "actual": actual, "expected": entry["expected"]})

        summary = "  ".join(
            f"{f}:{o[0].upper()}" for f, o in sorted(outcomes.items())
        )
        print(f"{path.name:24s} {elapsed:6.2f}s  {summary}")

    print()
    print(f"{'FIELD':22s} {'correct':>8s} {'wrong':>7s} {'missed':>7s} {'false+':>7s} {'acc':>7s}")
    print("-" * 62)

    scored = Counter()
    for field in sorted(per_field):
        counts = per_field[field]
        scored.update(counts)
        attempted = counts[CORRECT] + counts[WRONG] + counts[MISSED]
        accuracy = counts[CORRECT] / attempted if attempted else 0.0
        print(
            f"{field:22s} {counts[CORRECT]:8d} {counts[WRONG]:7d} "
            f"{counts[MISSED]:7d} {counts[FALSE_ACCEPT]:7d} {accuracy:6.0%}"
        )

    attempted = scored[CORRECT] + scored[WRONG] + scored[MISSED]
    overall = scored[CORRECT] / attempted if attempted else 0.0
    print("-" * 62)
    print(
        f"{'OVERALL':22s} {scored[CORRECT]:8d} {scored[WRONG]:7d} "
        f"{scored[MISSED]:7d} {scored[FALSE_ACCEPT]:7d} {overall:6.0%}"
    )
    # Reported separately. A single blended figure hides the only number that
    # matters commercially: whether the three always-present fields are
    # trustworthy enough for the operator to skip retyping them.
    print()
    for tier, fields in (("CORE (mrp, mfg, exp)", CORE_FIELDS),
                         ("CODES (batch, lot)", CODE_FIELDS)):
        tier_counts = Counter()
        for name in fields:
            tier_counts.update(per_field.get(name, Counter()))
        tried = tier_counts["correct"] + tier_counts["wrong"] + tier_counts["missed"]
        if not tried:
            continue
        print(
            f"{tier:24s} {tier_counts['correct']:3d} correct  "
            f"{tier_counts['wrong']:3d} wrong  {tier_counts['missed']:3d} missed  "
            f"{tier_counts['false accept']:3d} false+  -> {tier_counts['correct'] / tried:.0%}"
        )
    print(f"\n{len(rows)} images, {total_time / max(1, len(rows)):.1f}s/image")

    if false_accepts:
        print(f"\nFALSE ACCEPTS ({len(false_accepts)}) — the serious failure class:")
        for line in false_accepts:
            print(f"  {line}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
