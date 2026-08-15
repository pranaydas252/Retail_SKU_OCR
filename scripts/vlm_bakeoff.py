"""Score a local vision model against the ROI captures, via Ollama.

PP-OCRv5 puts only about a quarter of the required values into text at all on
these packs, so the extraction rules on top of it cannot reach the target no
matter how they are tuned. This measures whether a vision model closes that
gap on CPU, using the same ground truth and the same scoring as
measure_accuracy.py so the numbers are directly comparable.

    python scripts/vlm_bakeoff.py --model qwen2.5vl:3b
    python scripts/vlm_bakeoff.py --model qwen2.5vl:3b --limit 5

Latency is reported per image and is the thing to watch: on CPU a vision model
is seconds to minutes, against roughly 4s for the current pipeline.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from collections import Counter
from pathlib import Path

import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io  # noqa: E402

from PIL import Image  # noqa: E402

from app.services import normalizer  # noqa: E402

OLLAMA = "http://127.0.0.1:11434/api/generate"
TRUTH = Path("sample_data/roi_labels.json")

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


# Asks for exactly the fields the POC needs, in the normalized form the server
# already produces, and forbids guessing. "Not printed" has to be answerable,
# because a pack that genuinely lacks an MRP is a real case and inventing one
# is the worst failure class in this project.
PROMPT = """You are reading the statutory declaration panel on an Indian retail
product pack — a snack, food, beverage, medicine or household item photographed
in a store. The text may be an inkjet dot-matrix stamp on curved, glossy or
metallic packaging, and may be rotated or upside down.

Return ONLY these five values as strict JSON:

{"batchNumber": null, "manufacturingDate": null, "expiryDate": null, "lotCode": null, "mrp": null}

WHAT TO READ
- batchNumber — printed after BATCH / BATCH NO / B.NO / BN. Copy exactly.
- lotCode — printed after LOT / LOT NO. Copy exactly. If the pack prints a
  single combined "Lot / Batch No.", put it in batchNumber and leave lotCode null.
- manufacturingDate — MFG / MFD / PKD / PACKED / MANUFACTURED / DATE OF PACKAGING.
- expiryDate — EXP / EXPIRY / USE BY / USE BEFORE / BEST BEFORE.
- mrp — the maximum retail price.

WHAT TO IGNORE COMPLETELY
Ingredients, nutrition tables, marketing copy, addresses, customer care and
toll-free numbers, websites, email addresses, FSSAI and licence numbers, GTIN
and barcode digits, net weight or volume, and recycling or veg/non-veg marks.
None of these are ever an answer.

TRAPS THAT MATTER
- Unit price. Packs print the retail price beside a per-unit price, e.g.
  "Rs.60.00  Rs.0.30/g" or "₹319.00 (₹0.63/g)". The MRP is the LARGER figure.
  Anything followed by /g, /gm, /ml, /kg or "per g" is NOT the MRP.
- "(Inclusive of all taxes)" is boilerplate beside the price, never a value.
- Some packs print no field names at all — just a bare stamp such as
  "₹190/- ₹0.25/g SB3114C 08/10/25 07/10/26". When there are two dates and no
  labels, the EARLIER is manufacturing and the LATER is expiry.
- A shelf life such as "BEST BEFORE 9 MONTHS FROM PACKAGING" is not a date.
  Leave expiryDate null; do not calculate it.

READING CODES
Batch and lot codes have no fixed format, so they cannot be guessed from
context — read them character by character from your transcription. Keep
letters as letters and digits as digits: do not "correct" O to 0, I to 1 or
S to 5, and preserve any spaces, hyphens or slashes exactly as printed.

OUTPUT FORMAT
- Dates: "YYYY-MM-DD" when a day is printed, otherwise "YYYY-MM". Indian packs
  are day-first, so 03/06/2026 is 3 June 2026.
- mrp: plain number as a string, two decimals, no currency symbol: "232.00".
- Use null for anything not printed on this pack. A missing value is correct
  and useful; a guessed one is a serious error. Never invent or calculate.

Return the JSON object only, with no commentary."""


#: Longest side sent to the model.
#:
#: A vision model encodes a large image as thousands of tokens, and on CPU that
#: dominates the run time. Measured on one capture with qwen2.5vl:3b: 2692px
#: took 242s, 1600px 77s and 1100px 58s, with identical output every time.
#: Sending the full crop was paying four minutes for nothing.
#:
#: The best size is model-specific, so --max-side overrides this. A document
#: model may want more pixels than a general vision model, because the whole
#: task is reading small print.
MAX_SIDE = 1100


def encode(path: Path, max_side: int = MAX_SIDE) -> str:
    image = Image.open(path)
    image.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=92)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def normalize(field: str, value) -> str | None:
    """Put the model's answer into the server's own format.

    The model reads correctly but formats however it likes — it returned
    "03-06-2026" for a date the pipeline expresses as "2026-06-03". Scoring
    raw strings marked those wrong and made a model that had read the label
    perfectly look like it had failed.

    Normalizing here uses the same code the rule pipeline uses, so the model
    is judged on what it read rather than on how it chose to punctuate it.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-"}:
        return None

    if field.endswith("Date"):
        result = normalizer.normalize_date(text, "day")
    elif field == "mrp":
        result = normalizer.normalize_currency(text)
    else:
        result = normalizer.normalize_code(text)

    return result.value if result.ok else text


def ask(model: str, image: Path, timeout: int, max_side: int) -> tuple[dict, float]:
    payload = json.dumps({
        "model": model,
        "prompt": PROMPT,
        "images": [encode(image, max_side)],
        "stream": False,
        "format": "json",
        "options": {
            # Deterministic: this is a measurement, and sampling would make
            # the number unrepeatable.
            "temperature": 0,
            "num_predict": 400,
            # A label image is worth several thousand vision tokens on its
            # own. With the default 4096 window the image plus the prompt
            # overflowed and Ollama returned 400 before running anything.
            "num_ctx": 8192,
        },
    }).encode()

    request = urllib.request.Request(
        OLLAMA, data=payload, headers={"Content-Type": "application/json"}
    )

    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read())
    elapsed = time.perf_counter() - started

    try:
        return json.loads(body.get("response", "{}")), elapsed
    except json.JSONDecodeError:
        return {}, elapsed


def key(value) -> str:
    return "".join(c for c in str(value).upper() if c.isalnum())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen2.5vl:3b")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-side", type=int, default=MAX_SIDE,
                        help="longest side sent to the model")
    parser.add_argument("--json", help="write raw model output here")
    args = parser.parse_args()

    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    entries = truth["images"][: args.limit] if args.limit else truth["images"]

    per_field: dict[str, Counter] = {}
    false_accepts: list[str] = []
    rows: list[dict] = []
    total_time = 0.0

    for entry in entries:
        path = Path(entry["image"])
        if not path.exists():
            continue

        try:
            got, elapsed = ask(args.model, path, args.timeout, args.max_side)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"{path.name:32s} FAILED: {exc}", file=sys.stderr)
            continue

        total_time += elapsed
        outcomes = {}

        for field, want in entry["expected"].items():
            actual = normalize(field, got.get(field))

            if want is None:
                if actual is None:
                    continue
                outcome = "false accept"
                false_accepts.append(f"{path.name}: {field} = {actual!r}")
            elif actual is None:
                outcome = "missed"
            else:
                outcome = "correct" if key(want) == key(actual) else "wrong"

            outcomes[field] = outcome
            per_field.setdefault(field, Counter())[outcome] += 1

        rows.append({"image": path.name, "seconds": elapsed,
                     "model": got, "expected": entry["expected"]})
        print(
            f"{path.name:32s} {elapsed:6.1f}s  "
            + "  ".join(f"{f}:{o[0].upper()}" for f, o in sorted(outcomes.items())),
            flush=True,
        )

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
    overall = scored["correct"] / attempted if attempted else 0.0
    print("-" * 62)
    print(f"{'OVERALL':22s} {scored['correct']:8d} {scored['wrong']:7d} "
          f"{scored['missed']:7d} {scored['false accept']:7d} {overall:6.0%}")
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
    print(f"\n{args.model}: {len(rows)} images, {total_time / max(1, len(rows)):.1f}s/image")

    if false_accepts:
        print(f"\nFALSE ACCEPTS ({len(false_accepts)}):")
        for line in false_accepts:
            print(f"  {line}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
