"""Does cropping to the text region make the vision model's job easier?

Two measured facts motivate this:

* PaddleOCR-VL is perfect on a clean label and collapses into degenerate
  repetition on a cluttered pack shot. Every failure - "TTB", a LaTeX
  formula, "1.00 2.00 3.00..." - came from a busy panel.

* The ROI crop arrives around 2692px and is downscaled to 1100 before the
  model sees it, so a dot-matrix glyph lands at roughly 11px. Sending a
  larger image does not help: 1800px produced identical text for twice the
  time, because the extra pixels went to clutter.

Cropping attacks both at once. The same 1100px budget spends far more of
itself on the characters, and the ingredients, addresses and barcodes never
reach the model at all.

PP-OCRv5's DETECTOR does the cropping. Detection is the half of PP-OCRv5 that
works - it finds text reliably; recognition is what fails on dot-matrix - and
it is already running, so this costs nothing extra.

Three strategies:

    full        the whole ROI capture, unchanged - the control
    union       one box around every detected text region
    montage     only the regions near a field label, tiled into one image

Each is a single vision-model call, so latency stays comparable.

    python scripts/crop_bakeoff.py
    python scripts/crop_bakeoff.py --strategies full,union
"""

from __future__ import annotations

import argparse
import base64

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
import numpy as np  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import image_service  # noqa: E402
from app.services.field_extractor import extract_fields, load_field_specs  # noqa: E402
from app.services.ocr_service import OcrService, OcrToken  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")
OLLAMA = "http://127.0.0.1:11434/api/generate"
VL_MODEL = "hf.co/PaddlePaddle/PaddleOCR-VL-1.5-GGUF"

CORE_FIELDS = ("mrp", "manufacturingDate", "expiryDate")

#: Padding around a detected region, as a fraction of its height. Detection
#: boxes hug the glyphs, and a recogniser reads better with a little air -
#: descenders and the first character otherwise sit on the crop edge.
PAD = 0.35


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def detect(service: OcrService, image: np.ndarray, side: int):
    """Detect on a downscaled copy, return boxes in ORIGINAL coordinates.

    Detection is cheap at 960px and finds the same regions, but cropping the
    downscaled copy would defeat the whole point: the gain here is spending
    the vision model's fixed pixel budget on characters instead of packaging,
    and that only happens if the crop is taken from the full-resolution image.
    Cropping the 960px copy and then handing it to a model that wants 1100
    would upscale blur.
    """
    prepared = image_service.resize_for_ocr(
        image_service.build_variant(image, "original"), side
    )
    tokens = service.recognize(prepared)

    scale_x = image.shape[1] / prepared.shape[1]
    scale_y = image.shape[0] / prepared.shape[0]
    mapped = [
        OcrToken(
            text=t.text,
            x=int(t.x * scale_x),
            y=int(t.y * scale_y),
            width=int(t.width * scale_x),
            height=int(t.height * scale_y),
            confidence=t.confidence,
        )
        for t in tokens
    ]
    return mapped, prepared


def _pad_box(x, y, w, h, shape) -> tuple[int, int, int, int]:
    pad = int(h * PAD)
    height, width = shape[:2]
    return (
        max(0, x - pad),
        max(0, y - pad),
        min(width, x + w + pad),
        min(height, y + h + pad),
    )


def crop_union(image: np.ndarray, tokens: list[OcrToken]) -> np.ndarray:
    """One box around everything the detector found."""
    if not tokens:
        return image
    x0 = min(t.x for t in tokens)
    y0 = min(t.y for t in tokens)
    x1 = max(t.x + t.width for t in tokens)
    y1 = max(t.y + t.height for t in tokens)
    left, top, right, bottom = _pad_box(x0, y0, x1 - x0, y1 - y0, image.shape)
    return image[top:bottom, left:right]


def crop_montage(image: np.ndarray, tokens: list[OcrToken],
                 aliases: set[str]) -> np.ndarray:
    """Stack only the rows that carry a field label or sit beside one.

    A declaration panel is a handful of rows among dozens. Keeping the rows
    that matter and dropping ingredients, addresses and statutory text leaves
    the model a short, clean document - the shape it reads best.
    """
    if not tokens:
        return image

    def canonical(text: str) -> str:
        return "".join(c for c in text.upper() if c.isalnum())

    # A row is interesting if any token on it looks like one of the field
    # labels. The value then travels with it, since they share a line.
    interesting_rows = []
    for token in tokens:
        text = canonical(token.text)
        if any(text.startswith(alias) or alias in text for alias in aliases):
            interesting_rows.append(token)

    if not interesting_rows:
        return crop_union(image, tokens)

    bands: list[tuple[int, int]] = []
    for token in interesting_rows:
        _, top, _, bottom = _pad_box(
            token.x, token.y, token.width, token.height, image.shape
        )
        bands.append((top, bottom))

    # Merge overlapping bands so a row is never duplicated.
    bands.sort()
    merged: list[list[int]] = []
    for top, bottom in bands:
        if merged and top <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bottom)
        else:
            merged.append([top, bottom])

    # Trim dead width as well as dead height. The rows that matter rarely span
    # the frame: a capture keeps torn fragments of the previous panel on the
    # left and, on this pack, a rotated blue side-strip on the right. Cutting
    # those is free magnification, because the model's budget applies to the
    # LONGEST side and that side is the width.
    #
    # Rotated text is excluded from the extent. Side-strip legends are printed
    # at 90 degrees, so their boxes are taller than they are wide, and letting
    # one of them set the right edge would keep the whole strip.
    horizontal = [
        t for t in tokens
        if t.width >= t.height
        and any(top <= _centre(t) <= bottom for top, bottom in merged)
    ]
    if horizontal:
        pad = int(max(t.height for t in horizontal) * PAD)
        left = max(0, min(t.x for t in horizontal) - pad)
        right = min(image.shape[1], max(t.x + t.width for t in horizontal) + pad)
    else:
        left, right = 0, image.shape[1]

    strips = [image[top:bottom, left:right] for top, bottom in merged if bottom > top]
    strips = [s for s in strips if s.size]
    if not strips:
        return crop_union(image, tokens)
    return np.vstack(strips)


def _centre(token: OcrToken) -> float:
    return token.y + token.height / 2


def _safe_cut(target: int, occupied: list[tuple[int, int]], width: int) -> int:
    """Move a cut off any detected text box.

    Slicing a wide strip into columns is what buys the magnification, but a
    cut through the middle of a value destroys it — "319.00" split across two
    stacked chunks is unreadable, and unlike clutter that damage is silent.
    The detector already knows where the text is, so the cut is nudged to the
    nearest column that crosses nothing.
    """
    def clear(column: int) -> bool:
        return not any(left < column < right for left, right in occupied)

    if clear(target):
        return target

    # Search outward from the ideal position, bounded so a strip of solid
    # text falls back to the original cut rather than collapsing a chunk.
    for offset in range(1, width // 4):
        for candidate in (target - offset, target + offset):
            if 0 < candidate < width and clear(candidate):
                return candidate
    return target


def reflow(strips: np.ndarray, occupied: list[tuple[int, int]] | None = None,
           target_aspect: float = 1.0) -> np.ndarray:
    """Repack a wide, short image into a squarer one. UNSAFE on column layouts.

    Kept for the record and excluded from the default strategies, because
    inspecting its output showed it doing real damage: on a two-column label
    it put "MRP:", "Batch No:", "Date of Packaging:" and "Use By:" in the top
    chunk and 70.00, B2605 15, 15/05/26 and 14/05/27 in the bottom one,
    severing every label from its value.

    The cause is the safeguard itself. Cuts are snapped to whitespace, and on
    a label the widest whitespace column IS the gutter between the labels and
    their values — so the heuristic sought out the single most destructive
    place to cut. Widening the margin trim in crop_montage buys the same
    magnification without splitting anything.

    The vision model is given a fixed budget on its LONGEST side, so a
    2692x532 montage is downscaled by exactly as much as the 2692x1224
    original it came from: clutter is gone but the characters are no bigger.
    Cutting the strip into k columns and stacking them makes the image
    2692/k wide and k*532 tall, which fits the same budget while magnifying
    the text by k.

    k is chosen to bring the result closest to square, since that is what
    wastes least of a single-number budget. Reading order is preserved:
    chunks are stacked top to bottom, left to right, exactly as they would
    be read.
    """
    height, width = strips.shape[:2]
    if height <= 0 or width <= 0:
        return strips

    # Balanced when width/k == k*height*target_aspect.
    k = max(1, round((width / max(height, 1) / max(target_aspect, 0.01)) ** 0.5))
    if k <= 1:
        return strips

    chunk = width // k
    boxes = occupied or []
    cuts = [0]
    for index in range(1, k):
        cuts.append(_safe_cut(index * chunk, boxes, width))
    cuts.append(width)
    cuts = sorted(set(cuts))

    pieces = []
    for left, right in zip(cuts, cuts[1:]):
        piece = strips[:, left:right]
        if piece.size:
            pieces.append(piece)

    if len(pieces) < 2:
        return strips

    # Pad to a common width so they stack cleanly. White, because these are
    # printed labels on light substrate and a black band reads as an edge.
    widest = max(p.shape[1] for p in pieces)
    padded = [
        cv2.copyMakeBorder(p, 0, 0, 0, widest - p.shape[1],
                           cv2.BORDER_CONSTANT, value=(255, 255, 255))
        if p.shape[1] < widest else p
        for p in pieces
    ]
    return np.vstack(padded)


def ask_vl(image: np.ndarray, max_side: int, timeout: int) -> tuple[str, float]:
    resized = image_service.resize_for_ocr(image, max_side)
    ok, buffer = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return "", 0.0

    payload = json.dumps({
        "model": VL_MODEL,
        "prompt": "OCR:",
        "images": [base64.b64encode(buffer.tobytes()).decode("ascii")],
        "stream": False,
        "options": {
            "temperature": 0, "num_ctx": 8192, "num_predict": 220,
            "repeat_penalty": 1.15, "repeat_last_n": 64,
        },
    }).encode()

    request = urllib.request.Request(
        OLLAMA, data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = json.loads(response.read()).get("response", "")
    return text, time.perf_counter() - started


def fields_from_text(text: str) -> dict[str, str]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", type=int, default=960,
                        help="PP-OCRv5 detection side length")
    parser.add_argument("--max-side", type=int, default=1100)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--strategies", default="full,union,montage")
    parser.add_argument("--dump", type=Path,
                        help="write the cropped images here for inspection")
    args = parser.parse_args()

    strategies = args.strategies.split(",")
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    service = OcrService(get_settings().model_copy(
        update={"ocr_det_limit_side_len": args.side}
    ))

    aliases = set()
    for spec in load_field_specs():
        for alias in spec.aliases:
            cleaned = "".join(c for c in alias.upper() if c.isalnum())
            if len(cleaned) >= 3:
                aliases.add(cleaned)

    tally = {s: Counter() for s in strategies}
    timing = {s: 0.0 for s in strategies}
    pixels = {s: [] for s in strategies}

    for entry in truth["images"]:
        image = cv2.imread(entry["image"])
        if image is None:
            continue
        name = Path(entry["image"]).name

        tokens, _ = detect(service, image, args.side)

        # Every variant is cut from the full-resolution capture, so the only
        # downscale is the single one to max_side just before the model call.
        montage = crop_montage(image, tokens, aliases)
        occupied = [(t.x, t.x + t.width) for t in tokens]
        variants = {
            "full": image,
            "union": crop_union(image, tokens),
            "montage": montage,
            "reflow": reflow(montage, occupied),
        }

        for strategy in strategies:
            candidate = variants[strategy]
            if candidate is None or candidate.size == 0:
                candidate = image
            pixels[strategy].append(candidate.shape[0] * candidate.shape[1])

            if args.dump:
                args.dump.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(args.dump / f"{name}.{strategy}.jpg"), candidate)

            try:
                text, elapsed = ask_vl(candidate, args.max_side, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} {strategy}: {exc}", file=sys.stderr)
                continue
            timing[strategy] += elapsed

            got = fields_from_text(text)
            for field, want in entry["expected"].items():
                if want is None:
                    continue
                tier = "core" if field in CORE_FIELDS else "code"
                actual = got.get(field)
                if actual is None:
                    tally[strategy][f"{tier}.missed"] += 1
                elif key(actual) == key(want):
                    tally[strategy][f"{tier}.correct"] += 1
                else:
                    tally[strategy][f"{tier}.wrong"] += 1

        print(f"done {name}", flush=True)

    images = len(truth["images"])
    print(f"\n{'STRATEGY':10s} {'CORE':>13s} {'CODES':>13s} "
          f"{'megapixels':>11s} {'sec/img':>8s}")
    print("-" * 60)
    for strategy in strategies:
        cells = []
        for tier in ("core", "code"):
            counts = tally[strategy]
            correct = counts[f"{tier}.correct"]
            total = correct + counts[f"{tier}.wrong"] + counts[f"{tier}.missed"]
            cells.append(f"{correct}/{total} {correct / total:.0%}" if total else "-")
        mp = sum(pixels[strategy]) / max(1, len(pixels[strategy])) / 1e6
        print(f"{strategy:10s} {cells[0]:>13s} {cells[1]:>13s} "
              f"{mp:11.2f} {timing[strategy] / images:8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
