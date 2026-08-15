"""Do two recognisers, merged, beat either one alone?

PP-OCRv5 and PaddleOCR-VL fail on different packs. PP-OCRv5 reads printed
columns and loses inkjet stamps on curved or metallic film; PaddleOCR-VL reads
those stamps and returns only the pre-printed labels when the panel is
cluttered. Neither dominates, and each recovers values the other never sees:
on two captures PaddleOCR-VL read all three core fields that PP-OCRv5 classed
as never-recognised.

Both run in three to four seconds on CPU, so running both is affordable.

Four merge rules are measured, because the choice is not obvious:

    pp-first    take PP-OCRv5's value, fall back to PaddleOCR-VL
    vl-first    the reverse
    agree-only  emit a value only when both agree - maximum safety
    union       either engine correct counts as correct

"union" is not a shippable rule, since nothing at runtime knows which engine
was right. It is the ceiling: the best any merge could do, and the number that
says whether a smarter merge is worth building.

    python scripts/ensemble_accuracy.py
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import OcrService, OcrToken  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")
OLLAMA = "http://127.0.0.1:11434/api/generate"
VL_MODEL = "hf.co/PaddlePaddle/PaddleOCR-VL-1.5-GGUF"

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")
CODE_FIELDS = ("batchNumber", "lotCode")
ALL_FIELDS = CORE_FIELDS + CODE_FIELDS

MERGES = ("pp-first", "vl-first", "agree-only", "union")


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def run_paddle(service: OcrService, path: str, side: int) -> dict[str, str]:
    image = cv2.imread(path)
    if image is None:
        return {}
    prepared = image_service.resize_for_ocr(
        image_service.build_variant(image, "original"), side
    )
    return {
        name: c.normalized.value
        for name, c in extract_fields(service.recognize(prepared)).items()
        if c.normalized.ok
    }


def run_vl(path: str, max_side: int, timeout: int) -> dict[str, str]:
    """Transcribe with the vision model, then extract with the same rules.

    One synthetic token per transcribed line: the model has no coordinates to
    give, but it does preserve line structure, and on these packs a line is
    usually one field.
    """
    image = Image.open(path)
    image.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=92)

    payload = json.dumps({
        "model": VL_MODEL,
        "prompt": "OCR:",
        "images": [base64.b64encode(buffer.getvalue()).decode("ascii")],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_ctx": 8192,
            # Bounded so a degenerate repetition loop costs seconds, not
            # minutes. Every clean read finishes well inside this.
            "num_predict": 220,
            "repeat_penalty": 1.15,
            "repeat_last_n": 64,
        },
    }).encode()

    request = urllib.request.Request(
        OLLAMA, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = json.loads(response.read()).get("response", "")

    tokens = [
        OcrToken(text=line.strip(), x=0, y=index * 40,
                 width=max(len(line) * 10, 10), height=30, confidence=1.0)
        for index, line in enumerate(text.splitlines())
        if line.strip()
    ]
    return {
        name: c.normalized.value
        for name, c in extract_fields(tokens).items()
        if c.normalized.ok
    }


def merged(rule: str, field: str, pp: dict, vl: dict, want: str) -> str | None:
    a, b = pp.get(field), vl.get(field)

    if rule == "pp-first":
        return a if a is not None else b
    if rule == "vl-first":
        return b if b is not None else a
    if rule == "agree-only":
        return a if a is not None and b is not None and key(a) == key(b) else None

    # union: the ceiling. Credits a correct answer from either engine, which
    # no runtime rule could do without knowing which one to trust.
    if a is not None and key(a) == key(want):
        return a
    if b is not None and key(b) == key(want):
        return b
    return a if a is not None else b


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", type=int, default=960,
                        help="PP-OCRv5 detection side length")
    parser.add_argument("--max-side", type=int, default=1100,
                        help="longest side sent to the vision model")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    service = OcrService(get_settings().model_copy(
        update={"ocr_det_limit_side_len": args.side}
    ))

    tallies = {rule: {"core": Counter(), "code": Counter()} for rule in MERGES}
    agreements = Counter()
    pp_time = vl_time = 0.0
    scored = 0

    for entry in truth["images"]:
        path = entry["image"]
        if not Path(path).exists():
            continue

        started = time.perf_counter()
        pp = run_paddle(service, path, args.side)
        pp_time += time.perf_counter() - started

        started = time.perf_counter()
        try:
            vl = run_vl(path, args.max_side, args.timeout)
        except Exception as exc:  # noqa: BLE001 - a dead model must not abort the run
            print(f"  {Path(path).name}: vision model failed: {exc}", file=sys.stderr)
            vl = {}
        vl_time += time.perf_counter() - started

        for field, want in entry["expected"].items():
            if want is None:
                continue
            scored += 1
            tier = "core" if field in CORE_FIELDS else "code"

            a, b = pp.get(field), vl.get(field)
            if a is not None and b is not None:
                agreements["both answered"] += 1
                agreements["agreed" if key(a) == key(b) else "disagreed"] += 1
            elif a is not None or b is not None:
                agreements["one answered"] += 1
            else:
                agreements["neither answered"] += 1

            for rule in MERGES:
                got = merged(rule, field, pp, vl, want)
                if got is None:
                    tallies[rule][tier]["missed"] += 1
                elif key(got) == key(want):
                    tallies[rule][tier]["correct"] += 1
                else:
                    tallies[rule][tier]["wrong"] += 1

        print(f"done {Path(path).name}", flush=True)

    images = len(truth["images"])
    print(f"\n{scored} required values across {images} images")
    print(f"PP-OCRv5 {pp_time / images:.1f}s/image   "
          f"PaddleOCR-VL {vl_time / images:.1f}s/image   "
          f"combined {(pp_time + vl_time) / images:.1f}s/image\n")

    print(f"{'MERGE RULE':14s} {'CORE':>14s} {'CODES':>14s} {'wrong':>7s}")
    print("-" * 54)
    for rule in MERGES:
        cells = []
        wrong = 0
        for tier in ("core", "code"):
            counts = tallies[rule][tier]
            total = sum(counts[k] for k in ("correct", "wrong", "missed"))
            wrong += counts["wrong"]
            cells.append(
                f"{counts['correct']}/{total} {counts['correct'] / total:.0%}"
                if total else "-"
            )
        note = "  <- ceiling, not shippable" if rule == "union" else ""
        print(f"{rule:14s} {cells[0]:>14s} {cells[1]:>14s} {wrong:7d}{note}")

    print("\nAgreement between the two engines:")
    for name in ("both answered", "agreed", "disagreed", "one answered",
                 "neither answered"):
        print(f"  {name:18s} {agreements[name]:3d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
