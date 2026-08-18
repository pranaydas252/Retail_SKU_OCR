"""Do the confidence bands tell the operator anything true?

A band is only worth showing if it separates values that are right from values
that are wrong. Section 12 set the thresholds as engineering defaults and said
outright that they must be tuned against real label images. This measures them
against the gated corpus, which is what those images now are.

The failure this exists to catch is the quiet one. A scan where every value is
correct and four of five are flagged "Low confidence" is not a cautious scan,
it is a broken signal: operators who are warned on every scan stop reading the
warning, and then the band says nothing on the scan that genuinely is wrong.

Runs BOTH engines by default, because that is what production does. An earlier
version scored PP-OCRv5 alone, so the two-engine agreement signal never fired
and the thresholds were tuned against a world the app no longer lives in.

The VLM costs ~95s an image, so its tokens are cached to disk on first run and
reused after. Parameter sweeps are then free.

    python scripts/calibrate_confidence.py
    python scripts/calibrate_confidence.py --engine ocr
    python scripts/calibrate_confidence.py --refresh          # re-run the VLM
    python scripts/calibrate_confidence.py --sweep
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
from app.services import confidence, image_service  # noqa: E402
from app.services.field_extractor import load_config  # noqa: E402
from app.services.ocr_service import OcrToken, get_ocr_service  # noqa: E402
from app.services.scan_service import extract_and_score  # noqa: E402
from app.services.vlm_service import VlmService  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")
CACHE = Path(".cache/calibration_tokens.json")


def same(expected: str, actual: str) -> bool:
    def key(v: str) -> str:
        return "".join(c for c in str(v).upper() if c.isalnum())

    return key(expected) == key(actual)


def _dump(tokens: list[OcrToken]) -> list[dict]:
    return [
        {
            "text": t.text,
            "x": t.x,
            "y": t.y,
            "width": t.width,
            "height": t.height,
            "confidence": t.confidence,
            "variant": t.variant,
        }
        for t in tokens
    ]


def _load(raw: list[dict]) -> list[OcrToken]:
    return [OcrToken(**item) for item in raw]


def collect_tokens(engine: str, refresh: bool) -> dict[str, dict]:
    """OCR and VLM tokens per image, cached so parameter sweeps cost nothing."""
    cache: dict[str, dict] = {}
    if CACHE.exists() and not refresh:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))

    settings = get_settings()
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    ocr = get_ocr_service()

    vlm = None
    if engine == "both":
        vlm = VlmService(settings.model_copy(update={"vlm_enabled": True}))
        if not vlm.is_available():
            print(f"VLM unavailable ({settings.vlm_model}); is Ollama running?")
            sys.exit(1)

    wanted = [e for e in truth["images"] if e["expected"]]
    missing = [e for e in wanted if str(Path(e["image"])) not in cache]
    if missing:
        engines = "both engines" if vlm is not None else "OCR only"
        print(f"reading {len(missing)} image(s) not in cache ({engines})...")

    for index, entry in enumerate(truth["images"], 1):
        path = Path(entry["image"])
        key = str(path)
        if not entry["expected"] or key in cache:
            continue
        image = cv2.imread(str(path))
        if image is None:
            continue

        prepared = image_service.resize_for_ocr(
            image_service.build_variant(image, settings.variant_list[0]),
            settings.ocr_det_limit_side_len,
        )
        record = {"ocr": _dump(ocr.recognize(prepared)), "vlm": []}
        if vlm is not None:
            # Ollama has been observed to die outright partway through a
            # 20-image batch on the reference laptop -- not just drop the
            # connection, the process exits. Retry with a pause so a single
            # crash costs one image rather than the run; a failed image is
            # left UNCACHED so the next pass retries it, rather than poisoning
            # the cache with a VLM-less record that would then be scored as
            # "PP-OCRv5 found this alone".
            tokens = None
            for attempt in range(1, 4):
                try:
                    tokens = _dump(vlm.tokens(image))
                    break
                except Exception as exc:
                    print(f"  [{index}] {path.name}: VLM attempt {attempt} "
                          f"failed ({exc})")
                    time.sleep(10)
            if tokens is None:
                print(f"  [{index}] {path.name}: giving up, will retry next run")
                continue
            record["vlm"] = tokens
            print(
                f"  [{index}] {path.name}: {len(record['ocr'])} ocr"
                f" + {len(record['vlm'])} vlm tokens"
            )
        cache[key] = record

        # Written per image, not at the end. A full corpus pass is ~30 minutes
        # of VLM time, and a crash on image 11 used to discard the first ten.
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")

    return cache


def observe(cache: dict[str, dict], engine: str) -> list[dict]:
    """One row per value the pipeline actually produced."""
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    rows: list[dict] = []

    for entry in truth["images"]:
        key = str(Path(entry["image"]))
        record = cache.get(key)
        if not record or not entry["expected"]:
            continue

        ocr_tokens = _load(record["ocr"])
        vlm_tokens = _load(record["vlm"]) if engine == "both" else []
        fields, _ = extract_and_score(ocr_tokens, vlm_tokens or None)

        for name, field in fields.items():
            if field.value is None:
                continue
            want = entry["expected"].get(name)
            # A value produced where the pack prints none is wrong, not
            # unscored: it is the false-accept class, and its confidence is
            # exactly what decides whether an operator catches it.
            rows.append(
                {
                    "image": Path(key).name,
                    "field": name,
                    "score": float(field.confidence),
                    "correct": want is not None and same(want, field.value),
                    "engines": ",".join(field.engines or []) or "-",
                }
            )
    return rows


def report(rows: list[dict], high_t: float, review_t: float) -> None:
    def band_of(score: float) -> str:
        if score >= high_t:
            return "HIGH"
        if score >= review_t:
            return "REVIEW"
        return "LOW"

    print(f"\n{len(rows)} values produced across the corpus")
    print(f"CURRENT bands: HIGH >= {high_t}, REVIEW >= {review_t}\n")
    print(f"{'band':8s} {'values':>7s} {'correct':>8s} {'wrong':>6s} {'precision':>10s}")
    print("-" * 44)
    per_band: dict[str, Counter] = {}
    for row in rows:
        bucket = per_band.setdefault(band_of(row["score"]), Counter())
        bucket["correct" if row["correct"] else "wrong"] += 1
    for name in ("HIGH", "REVIEW", "LOW"):
        counts = per_band.get(name, Counter())
        total = counts["correct"] + counts["wrong"]
        precision = counts["correct"] / total if total else 0.0
        print(
            f"{name:8s} {total:7d} {counts['correct']:8d}"
            f" {counts['wrong']:6d} {precision:9.0%}"
        )

    correct_total = sum(1 for r in rows if r["correct"])
    print(
        f"\n{correct_total} of {len(rows)} produced values are correct"
        f" ({correct_total / len(rows):.0%})"
    )
    doubted = sum(1 for r in rows if r["correct"] and band_of(r["score"]) != "HIGH")
    print(f"{doubted} correct values are NOT in HIGH — every one is a false alarm")

    # -- does corroboration actually predict correctness? ------------------
    print("\ncorroboration vs. correctness:")
    print(
        f"{'engines':12s} {'values':>7s} {'correct':>8s}"
        f" {'wrong':>6s} {'precision':>10s}"
    )
    print("-" * 48)
    by_engine: dict[str, Counter] = {}
    for row in rows:
        bucket = by_engine.setdefault(row["engines"], Counter())
        bucket["correct" if row["correct"] else "wrong"] += 1
    for name in sorted(by_engine):
        counts = by_engine[name]
        total = counts["correct"] + counts["wrong"]
        print(
            f"{name:12s} {total:7d} {counts['correct']:8d}"
            f" {counts['wrong']:6d} {counts['correct'] / total:9.0%}"
        )

    # -- score distribution, which is what a threshold has to work with ----
    print("\nscore distribution:")
    print(f"{'range':>12s} {'correct':>8s} {'wrong':>6s}")
    print("-" * 28)
    edges = [0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]
    for low, high in zip(edges, edges[1:]):
        inside = [r for r in rows if low <= r["score"] < high]
        if not inside:
            continue
        right = sum(1 for r in inside if r["correct"])
        print(f" {low:.2f}-{high:.2f} {right:8d} {len(inside) - right:6d}")

    # -- candidate thresholds ----------------------------------------------
    print("\ncandidate HIGH thresholds:")
    print(
        f"{'threshold':>10s} {'correct in HIGH':>16s}"
        f" {'wrong in HIGH':>14s} {'precision':>10s}"
    )
    print("-" * 54)
    best = None
    for threshold in sorted({round(r["score"], 2) for r in rows}):
        admitted = [r for r in rows if r["score"] >= threshold]
        right = sum(1 for r in admitted if r["correct"])
        wrong = len(admitted) - right
        precision = right / len(admitted) if admitted else 0.0
        print(f"{threshold:10.2f} {right:16d} {wrong:14d} {precision:9.0%}")
        if wrong == 0 and (best is None or right > best[1]):
            best = (threshold, right)

    if best:
        print(
            f"\nHighest-coverage threshold admitting NO wrong value: {best[0]}"
            f" ({best[1]} of {correct_total} correct values reach HIGH)"
        )


def sweep_agreement(cache: dict[str, dict], high_t: float) -> None:
    """What the two-engine bonus is worth, holding everything else fixed."""
    print(f"\n\nagreement bonus sweep (HIGH >= {high_t}):")
    print(
        f"{'bonus':>7s} {'correct HIGH':>13s} {'wrong HIGH':>11s}"
        f" {'false alarms':>13s}"
    )
    print("-" * 48)

    base = dict(load_config())
    original = confidence.load_config
    try:
        for bonus in (0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75):
            patched = {**base, "engine_agreement_bonus": bonus}
            confidence.load_config = lambda p=patched: p
            rows = observe(cache, "both")
            high = [r for r in rows if r["score"] >= high_t]
            right = sum(1 for r in high if r["correct"])
            wrong = len(high) - right
            alarms = sum(1 for r in rows if r["correct"] and r["score"] < high_t)
            print(f"{bonus:7.2f} {right:13d} {wrong:11d} {alarms:13d}")
    finally:
        confidence.load_config = original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("both", "ocr"), default="both")
    parser.add_argument(
        "--refresh", action="store_true",
        help="re-run the engines instead of using the cache",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="also sweep the two-engine agreement bonus",
    )
    parser.add_argument("--json", help="write the raw score/outcome pairs here")
    args = parser.parse_args()

    settings = get_settings()
    cache = collect_tokens(args.engine, args.refresh)
    rows = observe(cache, args.engine)
    if not rows:
        print("Nothing scored.")
        return 1

    print(f"\nengine mode: {args.engine}")
    report(rows, settings.confidence_high_threshold,
           settings.confidence_review_threshold)

    if args.sweep and args.engine == "both":
        sweep_agreement(cache, settings.confidence_high_threshold)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
