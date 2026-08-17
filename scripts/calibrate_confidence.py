"""Do the confidence bands tell the operator anything true?

A band is only worth showing if it separates values that are right from values
that are wrong. Section 12 set the thresholds as engineering defaults and said
outright that they must be tuned against real label images. This measures them
against the gated corpus, which is what those images now are.

The failure this exists to catch is the quiet one. A scan where every value is
correct and four of five are flagged "Low confidence" is not a cautious scan,
it is a broken signal: operators who are warned on every scan stop reading the
warning, and then the band says nothing on the scan that genuinely is wrong.

Reports, per band and per field:

    precision   of the values in this band, how many were correct
    coverage    how much of the corpus lands here

and searches for thresholds that maximise the thing that actually matters —
correct values reaching HIGH without any wrong value reaching it with them.

    python scripts/calibrate_confidence.py
    python scripts/calibrate_confidence.py --json out.json
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

import cv2  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.ocr_service import get_ocr_service  # noqa: E402
from app.services.scan_service import extract_and_score  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")


def same(expected: str, actual: str) -> bool:
    def key(v: str) -> str:
        return "".join(c for c in str(v).upper() if c.isalnum())

    return key(expected) == key(actual)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write the raw score/outcome pairs here")
    args = parser.parse_args()

    settings = get_settings()
    ocr = get_ocr_service()
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))

    # (field, score, correct) for every value the pipeline actually produced.
    observations: list[tuple[str, float, bool]] = []

    for entry in truth["images"]:
        path = Path(entry["image"])
        image = cv2.imread(str(path))
        if image is None or not entry["expected"]:
            continue

        prepared = image_service.resize_for_ocr(
            image_service.build_variant(image, settings.variant_list[0]),
            settings.ocr_det_limit_side_len,
        )
        fields, _ = extract_and_score(ocr.recognize(prepared))

        for name, field in fields.items():
            if field.value is None:
                continue
            want = entry["expected"].get(name)
            # A value produced where the pack prints none is wrong, not
            # unscored: it is the false-accept class, and its confidence is
            # exactly what decides whether an operator catches it.
            correct = want is not None and same(want, field.value)
            observations.append((name, float(field.confidence), correct))

    if not observations:
        print("Nothing scored.")
        return 1

    print(f"{len(observations)} values produced across the corpus\n")

    # -- what the current thresholds do ------------------------------------
    high_t = settings.confidence_high_threshold
    review_t = settings.confidence_review_threshold

    def band_of(score: float) -> str:
        if score >= high_t:
            return "HIGH"
        if score >= review_t:
            return "REVIEW"
        return "LOW"

    print(f"CURRENT bands: HIGH >= {high_t}, REVIEW >= {review_t}\n")
    print(f"{'band':8s} {'values':>7s} {'correct':>8s} {'wrong':>6s} {'precision':>10s}")
    print("-" * 44)
    per_band: dict[str, Counter] = {}
    for _, score, correct in observations:
        per_band.setdefault(band_of(score), Counter())["correct" if correct else "wrong"] += 1
    for name in ("HIGH", "REVIEW", "LOW"):
        counts = per_band.get(name, Counter())
        total = counts["correct"] + counts["wrong"]
        precision = counts["correct"] / total if total else 0.0
        print(f"{name:8s} {total:7d} {counts['correct']:8d} {counts['wrong']:6d} "
              f"{precision:9.0%}")

    correct_total = sum(1 for _, _, c in observations if c)
    print(f"\n{correct_total} of {len(observations)} produced values are correct "
          f"({correct_total / len(observations):.0%})")

    # The headline failure: correct values the operator is told to distrust.
    doubted = sum(
        1 for _, score, correct in observations if correct and band_of(score) != "HIGH"
    )
    print(f"{doubted} correct values are NOT in HIGH — every one is a false alarm")

    # -- score distribution, which is what a threshold has to work with ----
    print("\nscore distribution:")
    print(f"{'range':>12s} {'correct':>8s} {'wrong':>6s}")
    print("-" * 28)
    edges = [0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]
    for low, high in zip(edges, edges[1:]):
        inside = [(s, c) for _, s, c in observations if low <= s < high]
        if not inside:
            continue
        right = sum(1 for _, c in inside if c)
        print(f"{low:5.2f}-{high:<5.2f} {right:8d} {len(inside) - right:6d}")

    # -- search for a better HIGH threshold --------------------------------
    #
    # The objective is deliberately asymmetric. Section 24 rates a wrong value
    # above a missing one, so a wrong value shown as HIGH is the expensive
    # mistake; a correct value shown as REVIEW only costs a glance. Candidates
    # are therefore ranked by how many correct values reach HIGH, subject to
    # admitting as few wrong ones as possible.
    print("\ncandidate HIGH thresholds:")
    print(f"{'threshold':>10s} {'correct in HIGH':>16s} {'wrong in HIGH':>14s} "
          f"{'precision':>10s}")
    print("-" * 54)
    scores = sorted({round(s, 2) for _, s, _ in observations})
    best = None
    for candidate in scores:
        promoted = [(s, c) for _, s, c in observations if s >= candidate]
        right = sum(1 for _, c in promoted if c)
        wrong = len(promoted) - right
        precision = right / len(promoted) if promoted else 0.0
        print(f"{candidate:10.2f} {right:16d} {wrong:14d} {precision:9.0%}")
        # Perfect precision preferred; among those, the most coverage.
        if wrong == 0 and (best is None or right > best[1]):
            best = (candidate, right)

    if best:
        threshold, reached = best
        print(f"\nHighest-coverage threshold admitting NO wrong value: {threshold:.2f} "
              f"({reached} of {correct_total} correct values reach HIGH)")
    else:
        print("\nNo threshold separates correct from wrong cleanly on this corpus.")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [{"field": f, "score": s, "correct": c} for f, s, c in observations],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
