"""Write a ground-truth skeleton for a freshly collected sample set.

Transcribing a corpus by hand is slow enough without also typing JSON
structure, and a hand-built file is where a mistyped key silently excludes an
image from scoring. This lists what is actually on disk and emits one entry per
image with the field names already in place.

    python scripts/scaffold_ground_truth.py
    python scripts/scaffold_ground_truth.py --dir sample_data/roi --out sample_data/roi_labels.json

Every field starts as null, which means "the pack does not print this". Change
it to the printed value, or delete the key entirely if the capture is not
legible enough to assert a truth — an absent key is not scored either way, so a
guess is neither rewarded nor punished.

Existing entries are preserved. Re-running after adding more captures appends
the new ones and leaves transcribed values alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DIR = "sample_data/roi"
DEFAULT_OUT = "sample_data/roi_labels.json"

FIELDS = ["batchNumber", "manufacturingDate", "expiryDate", "lotCode", "mrp"]

README = [
    "Ground truth for ROI-cropped captures taken through the app itself.",
    "",
    "This is the input the backend actually receives: the app gates capture on",
    "a readiness score, crops to the ROI window and deskews before uploading.",
    "",
    "Conventions:",
    "  value   - the pack prints this field and it reads exactly this, already",
    "            in the normalized form the server should produce (YYYY-MM or",
    "            YYYY-MM-DD for dates, plain decimal for money).",
    "  null    - the field is genuinely ABSENT from the pack. Extracting",
    "            anything here is a false accept, the worst failure class in",
    "            this project (CLAUDE.md section 24).",
    "  omitted - not legible enough in the capture to assert a truth. Not",
    "            scored either way, so a guess is neither rewarded nor",
    "            punished.",
    "",
    "Transcribe exactly what is printed, including any character the OCR is",
    "likely to confuse. This file is the arbiter of O/0, I/1, S/5, B/8, G/6",
    "and Z/2 disputes.",
    "",
    "On packs with a bare stamp and no field names: batch number and",
    "manufacturing date are always present, and of two dates the earlier is",
    "manufacture. A HH:MM value is a clock time, not a date.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_DIR, help="where the captures are")
    parser.add_argument("--out", default=DEFAULT_OUT, help="ground-truth file to write")
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"No such directory: {directory}")
        return 1

    images = sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not images:
        print(f"No captures in {directory}. Collect some first — see {directory.parent}/README.md")
        return 1

    out = Path(args.out)
    existing: dict[str, dict] = {}
    if out.exists():
        previous = json.loads(out.read_text(encoding="utf-8"))
        existing = {entry["image"]: entry for entry in previous.get("images", [])}

    entries = []
    added = 0
    for image in images:
        key = image.as_posix()
        if key in existing:
            entries.append(existing[key])
            continue
        entries.append({"image": key, "expected": {field: None for field in FIELDS}})
        added += 1

    out.write_text(
        json.dumps({"_readme": README, "images": entries}, indent=2) + "\n",
        encoding="utf-8",
    )

    kept = len(entries) - added
    print(f"{out}: {len(entries)} images ({added} new, {kept} already transcribed)")
    if added:
        print("\nFill in the values. A field left null asserts the pack does not print it,")
        print("so delete the key instead when a capture is simply not legible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
