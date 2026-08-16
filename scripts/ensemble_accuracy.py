"""Does the second engine earn its latency?

Runs the real pipeline three ways over the same corpus and scores all three
against the same ground truth:

    ocr        PP-OCRv5 alone — what ships today
    vlm        the vision-language model alone
    ensemble   both, merged by app/services/ensemble.py

Everything goes through `scan_service.extract_and_score`, which is the code the
server actually runs. A measurement taken through a private path would answer a
question nobody asked.

The interesting column is not the headline percentage. It is WRONG, because
section 24 rates a confidently wrong value above a missing one: a wrong expiry
date reaches a shelf, a missing one reaches an operator. A merge that trades
five misses for three wrong answers is a worse merge even when the percentage
improves.

    python scripts/ensemble_accuracy.py
    python scripts/ensemble_accuracy.py --limit 5      # a quick look
    python scripts/ensemble_accuracy.py --json out.json

Findings — the reasons behind individual failures — are written to
FINDINGS.md, so a fix can be planned after the cycle rather than chased
mid-measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from app.services.vlm_service import VlmService  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")
FINDINGS = Path("FINDINGS.md")

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")
CODE_FIELDS = ("batchNumber", "lotCode")
ALL_FIELDS = CORE_FIELDS + CODE_FIELDS

CORRECT, WRONG, MISSED, FALSE_ACCEPT = "correct", "wrong", "missed", "false accept"


def same(expected: str, actual: str) -> bool:
    def key(v: str) -> str:
        return "".join(ch for ch in v.upper() if ch.isalnum())

    return key(expected) == key(actual)


def classify(expected: str | None, actual: str | None) -> str | None:
    """None means the field is not scored — truth was not asserted."""
    if expected is None:
        return None if actual is None else FALSE_ACCEPT
    if actual is None:
        return MISSED
    return CORRECT if same(expected, actual) else WRONG


def tally(rows: list[dict], strategy: str) -> dict:
    counts: Counter[str] = Counter()
    tiers = {"core": Counter(), "code": Counter()}

    for row in rows:
        for field, outcome in row[strategy]["outcomes"].items():
            counts[outcome] += 1
            tier = "core" if field in CORE_FIELDS else "code"
            tiers[tier][outcome] += 1

    def pct(c: Counter) -> int:
        total = c[CORRECT] + c[WRONG] + c[MISSED]
        return round(100 * c[CORRECT] / total) if total else 0

    return {
        "counts": counts,
        "core": tiers["core"],
        "code": tiers["code"],
        "corePct": pct(tiers["core"]),
        "codePct": pct(tiers["code"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="score only the first N images")
    parser.add_argument("--json", help="write raw per-field results here")
    args = parser.parse_args()

    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    entries = truth["images"][: args.limit] if args.limit else truth["images"]

    settings = get_settings()
    ocr = get_ocr_service()
    vlm = VlmService(settings.model_copy(update={"vlm_enabled": True}))

    if not vlm.is_available():
        print(f"VLM runtime or model unavailable: {settings.vlm_model}")
        return 1

    print(f"{len(entries)} images | primary {settings.ocr_rec_model_name} "
          f"| secondary {settings.vlm_model} @ {settings.vlm_max_side}px\n")

    rows: list[dict] = []
    for index, entry in enumerate(entries, 1):
        path = Path(entry["image"])
        expected = entry["expected"]
        if not path.exists():
            print(f"  missing: {path}")
            continue
        if not expected:
            print(f"[{index}/{len(entries)}] {path.name}: nothing asserted, skipped")
            continue

        image = cv2.imread(str(path))

        started = time.perf_counter()
        ocr_tokens = ocr.recognize(
            image_service.resize_for_ocr(image, settings.ocr_det_limit_side_len)
        )
        ocr_ms = int((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        try:
            vlm_tokens = vlm.tokens(image)
        except Exception as exc:  # a model failure must not end the run
            print(f"    VLM failed on {path.name}: {exc}")
            vlm_tokens = []
        vlm_ms = int((time.perf_counter() - started) * 1000)

        row: dict = {"image": path.name, "ocrMs": ocr_ms, "vlmMs": vlm_ms}
        for strategy, primary, secondary in (
            ("ocr", ocr_tokens, None),
            ("vlm", vlm_tokens, None),
            ("ensemble", ocr_tokens, vlm_tokens),
        ):
            fields, _ = extract_and_score(primary, secondary)
            values = {f: fields[f].value if f in fields else None for f in ALL_FIELDS}
            outcomes = {
                f: classify(expected.get(f, None) if f in expected else None, values[f])
                for f in ALL_FIELDS
                if f in expected
            }
            row[strategy] = {
                "values": values,
                "outcomes": {f: o for f, o in outcomes.items() if o is not None},
            }

        rows.append(row)
        marks = " ".join(
            f"{s}:{sum(1 for o in row[s]['outcomes'].values() if o == CORRECT)}"
            for s in ("ocr", "vlm", "ensemble")
        )
        print(f"[{index}/{len(entries)}] {path.name:34s} "
              f"ocr {ocr_ms/1000:5.1f}s  vlm {vlm_ms/1000:6.1f}s   correct: {marks}")

    if not rows:
        print("Nothing scored.")
        return 1

    print(f"\n{'strategy':10s} {'core':>6s} {'codes':>6s} "
          f"{'correct':>8s} {'wrong':>6s} {'missed':>7s} {'false+':>7s}")
    print("-" * 56)
    summary = {}
    for strategy in ("ocr", "vlm", "ensemble"):
        t = tally(rows, strategy)
        summary[strategy] = t
        c = t["counts"]
        print(f"{strategy:10s} {t['corePct']:5d}% {t['codePct']:5d}% "
              f"{c[CORRECT]:8d} {c[WRONG]:6d} {c[MISSED]:7d} {c[FALSE_ACCEPT]:7d}")

    ocr_ms = sum(r["ocrMs"] for r in rows) / len(rows)
    vlm_ms = sum(r["vlmMs"] for r in rows) / len(rows)
    print(f"\nper image: PP-OCRv5 {ocr_ms/1000:.1f}s, VLM {vlm_ms/1000:.1f}s, "
          f"both {(ocr_ms + vlm_ms)/1000:.1f}s")

    # Section 24: a wrong value is worse than a missing one, so the merge is
    # judged on whether it added wrong answers, not only on the percentage.
    added_wrong = summary["ensemble"]["counts"][WRONG] - summary["ocr"]["counts"][WRONG]
    added_false = (summary["ensemble"]["counts"][FALSE_ACCEPT]
                   - summary["ocr"]["counts"][FALSE_ACCEPT])
    print(f"\nensemble vs PP-OCRv5 alone: "
          f"{added_wrong:+d} wrong, {added_false:+d} false accepts, "
          f"{summary['ensemble']['corePct'] - summary['ocr']['corePct']:+d} core points")

    write_findings(rows, summary)
    print(f"\nFindings written to {FINDINGS}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"Raw results written to {args.json}")
    return 0


def write_findings(rows: list[dict], summary: dict) -> None:
    """Record every disagreement worth looking at after the cycle."""
    lines = [
        "# Findings",
        "",
        "Generated by `scripts/ensemble_accuracy.py`. Each entry is something to",
        "fix or decide after the measurement cycle, not during it.",
        "",
        "## Summary",
        "",
        "| strategy | core | codes | correct | wrong | missed | false accepts |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for strategy in ("ocr", "vlm", "ensemble"):
        t = summary[strategy]
        c = t["counts"]
        lines.append(
            f"| {strategy} | {t['corePct']}% | {t['codePct']}% | "
            f"{c[CORRECT]} | {c[WRONG]} | {c[MISSED]} | {c[FALSE_ACCEPT]} |"
        )

    sections = {
        "Wrong values — the serious class (section 24)": [],
        "False accepts — a value on a pack that prints none": [],
        "The VLM answered where PP-OCRv5 did not": [],
        "The VLM contradicted PP-OCRv5": [],
        "Still missed by both engines": [],
    }

    for row in rows:
        for field, outcome in row["ensemble"]["outcomes"].items():
            ocr_v = row["ocr"]["values"][field]
            vlm_v = row["vlm"]["values"][field]
            ens_v = row["ensemble"]["values"][field]
            where = f"`{row['image']}` **{field}**"

            if outcome == WRONG:
                sections["Wrong values — the serious class (section 24)"].append(
                    f"- {where}: ensemble said `{ens_v}` "
                    f"(ocr `{ocr_v}`, vlm `{vlm_v}`)"
                )
            elif outcome == FALSE_ACCEPT:
                sections["False accepts — a value on a pack that prints none"].append(
                    f"- {where}: produced `{ens_v}` (ocr `{ocr_v}`, vlm `{vlm_v}`)"
                )
            elif outcome == MISSED:
                sections["Still missed by both engines"].append(f"- {where}")

            if ocr_v is None and vlm_v is not None:
                sections["The VLM answered where PP-OCRv5 did not"].append(
                    f"- {where}: vlm `{vlm_v}` — {outcome}"
                )
            elif ocr_v and vlm_v and not same(ocr_v, vlm_v):
                sections["The VLM contradicted PP-OCRv5"].append(
                    f"- {where}: ocr `{ocr_v}` vs vlm `{vlm_v}` — {outcome}"
                )

    for title, items in sections.items():
        lines += ["", f"## {title} ({len(items)})", ""]
        lines += items or ["_none_"]

    FINDINGS.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
