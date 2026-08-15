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

from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import get_ocr_service  # noqa: E402

GROUND_TRUTH = Path("sample_data/real_labels.json")

CORRECT, WRONG, MISSED, FALSE_ACCEPT = "correct", "wrong", "missed", "false accept"


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
    args = parser.parse_args()

    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    ocr = get_ocr_service()

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

        started = time.perf_counter()
        tokens = ocr.recognize(image)
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
