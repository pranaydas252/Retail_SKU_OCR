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

OLLAMA = "http://127.0.0.1:11434/api/generate"
TRUTH = Path("sample_data/roi_labels.json")

# Asks for exactly the fields the POC needs, in the normalized form the server
# already produces, and forbids guessing. "Not printed" has to be answerable,
# because a pack that genuinely lacks an MRP is a real case and inventing one
# is the worst failure class in this project.
PROMPT = """You are reading a photograph of a printed product label from an Indian retail pack.

Extract ONLY what is actually printed. Do not infer, calculate or guess.

Return strict JSON with exactly these keys:
{"batchNumber": ..., "manufacturingDate": ..., "expiryDate": ..., "lotCode": ..., "mrp": ...}

Rules:
- Use null for any field not printed on this label.
- Dates: return YYYY-MM-DD if a day is printed, otherwise YYYY-MM.
- mrp: the maximum retail price as a plain number like "232.00". Ignore any
  per-gram or per-ml unit price such as "0.30/g".
- batchNumber / lotCode: copy the code exactly as printed.
- Return the JSON object only, with no commentary."""


def encode(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def ask(model: str, image: Path, timeout: int) -> tuple[dict, float]:
    payload = json.dumps({
        "model": model,
        "prompt": PROMPT,
        "images": [encode(image)],
        "stream": False,
        "format": "json",
        # Deterministic: this is a measurement, and sampling would make the
        # number unrepeatable.
        "options": {"temperature": 0, "num_predict": 400},
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
            got, elapsed = ask(args.model, path, args.timeout)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"{path.name:32s} FAILED: {exc}", file=sys.stderr)
            continue

        total_time += elapsed
        outcomes = {}

        for field, want in entry["expected"].items():
            actual = got.get(field)
            if isinstance(actual, str) and not actual.strip():
                actual = None

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
