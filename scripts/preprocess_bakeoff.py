"""Preprocessing bake-off against the ROI captures.

The pipeline currently runs one variant, `original`, and stops at the first
that returns any text at all. Two consequences were never tested:

* CLAUDE.md section 7 asks for several variants to be compared. They never
  were, because the loop short-circuits on "any text found" rather than on
  "the fields we want found".
* Dot-matrix and inkjet printing is made of separated dots. A morphological
  close joins them into strokes, which is the standard remedy and is exactly
  the failure mode on cans and sachets here.

Rotation is included because several packs are photographed at 90 or 180
degrees, and PaddleOCR's own orientation classifier made no difference.

    python scripts/preprocess_bakeoff.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.services.field_extractor import extract_fields  # noqa: E402
from app.services.ocr_service import get_ocr_service  # noqa: E402

TRUTH = Path("sample_data/roi_labels.json")
SIDE = 960


def _resize(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= SIDE:
        return image
    scale = SIDE / longest
    return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def v_original(img):
    return _resize(img)


def v_clahe(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
    return _resize(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR))


def v_dotmatrix(img):
    """Join the dots of an inkjet character into a stroke.

    Dot-matrix glyphs are separated ink dots. A recogniser trained on solid
    type sees noise; closing with a small ellipse merges the dots into the
    stroke the character is meant to be. Upscaling first matters because the
    kernel has to be larger than the dot gap.
    """
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
    g = cv2.resize(g, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    g = cv2.morphologyEx(g, cv2.MORPH_CLOSE, kernel)
    g = cv2.bilateralFilter(g, 5, 60, 60)
    return _resize(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR))


def v_adaptive(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
    g = cv2.adaptiveThreshold(
        g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    return _resize(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR))


def v_unsharp(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    blur = cv2.GaussianBlur(g, (0, 0), 3)
    g = cv2.addWeighted(g, 1.8, blur, -0.8, 0)
    return _resize(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR))


VARIANTS = {
    "original": v_original,
    "clahe": v_clahe,
    "dotmatrix": v_dotmatrix,
    "adaptive": v_adaptive,
    "unsharp": v_unsharp,
}

ROTATIONS = {
    0: lambda i: i,
    90: lambda i: cv2.rotate(i, cv2.ROTATE_90_CLOCKWISE),
    180: lambda i: cv2.rotate(i, cv2.ROTATE_180),
    270: lambda i: cv2.rotate(i, cv2.ROTATE_90_COUNTERCLOCKWISE),
}


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def main() -> int:
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    ocr = get_ocr_service()

    # variant -> rotation -> correct count
    tally: dict[str, dict[int, int]] = {v: {r: 0 for r in ROTATIONS} for v in VARIANTS}
    timing: dict[str, float] = {v: 0.0 for v in VARIANTS}
    scored = 0

    for entry in truth["images"]:
        image = cv2.imread(entry["image"])
        if image is None:
            continue
        wanted = {f: v for f, v in entry["expected"].items() if v is not None}
        scored += len(wanted)

        for vname, vfn in VARIANTS.items():
            for rot, rfn in ROTATIONS.items():
                prepared = vfn(rfn(image))
                started = time.perf_counter()
                tokens = ocr.recognize(prepared)
                timing[vname] += time.perf_counter() - started

                got = {
                    f: c.normalized.value
                    for f, c in extract_fields(tokens).items()
                    if c.normalized.ok
                }
                hits = sum(
                    1 for f, v in wanted.items()
                    if f in got and key(got[f]) == key(v)
                )
                tally[vname][rot] += hits

        print(f"done {Path(entry['image']).name}", flush=True)

    print(f"\n{scored} scorable values across {len(truth['images'])} images\n")
    print(f"{'VARIANT':12s} {'rot0':>6s} {'rot90':>6s} {'rot180':>6s} {'rot270':>6s} "
          f"{'BEST-OF':>8s} {'sec/img':>8s}")
    print("-" * 62)

    for vname in VARIANTS:
        row = tally[vname]
        best = max(row.values())
        print(
            f"{vname:12s} {row[0]:6d} {row[90]:6d} {row[180]:6d} {row[270]:6d} "
            f"{best:8d} {timing[vname] / (len(truth['images']) * 4):8.1f}"
        )

    print("\nPer-value accuracy of the best single (variant, rotation):")
    best_combo = max(
        ((v, r, tally[v][r]) for v in VARIANTS for r in ROTATIONS),
        key=lambda t: t[2],
    )
    print(f"  {best_combo[0]} @ {best_combo[1]}deg -> {best_combo[2]}/{scored} "
          f"= {best_combo[2] / scored:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
