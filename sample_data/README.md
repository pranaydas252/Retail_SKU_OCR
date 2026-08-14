# Sample Data

Test images for the OCR pipeline. **Image binaries are git-ignored** — only this file, the folder structure, and `expected.json` ground truth are tracked.

## Approach: zero-shot

PaddleOCR is used **zero-shot**. No fine-tuning, no training data, no curated public datasets.

PP-OCRv5 is already a strong general recognizer. The variable in this project is the extraction and normalization logic layered on top of it, not the OCR model. Building dataset machinery before knowing where PaddleOCR actually fails would be effort spent on the wrong layer.

So this directory holds one thing: **real D-Mart SKU label images shot on the TC22**, plus their ground truth.

Until those exist there is no accuracy gate to run. Extraction logic is built and unit-tested against hand-written token fixtures in `tests/`; the accuracy number arrives with the images.

## Buckets

Per `CLAUDE.md` section 25:

```text
sample_data/
├── good/                 # Clean, well-lit, flat labels — the baseline
├── low_light/            # Underexposed
├── blur/                 # Motion or focus blur
├── glare/                # Specular highlights, reflective film
├── curved_packaging/     # Bottles, pouches, cylindrical packs
└── rotated/              # Off-axis, upside-down, skewed
```

Accuracy is reported **per bucket**. A pipeline that scores well on `good/` and collapses on `glare/` is not ready.

Aim for a spread across buckets rather than volume. Twenty images per bucket that genuinely exhibit the condition beat two hundred near-duplicates of the easy case.

## Ground truth

Each bucket carries an `expected.json`:

```json
[
  {
    "image": "sku_001.jpg",
    "expected": {
      "batchNumber": "A23C91",
      "manufacturingDate": "2026-07",
      "expiryDate": "2028-06",
      "lotCode": "K18P2",
      "mrp": "245.00"
    }
  }
]
```

Rules:

- Use `null` for a field genuinely absent from the label. **Do not guess.**
- A field marked `null` that the extractor "finds" is a **false accept** — the most serious failure class in this project. A wrong expiry date is worse than a recapture request.
- Dates are recorded in the normalized form the server should produce: `YYYY-MM` for month/year, `YYYY-MM-DD` for full dates.
- Transcribe exactly what is printed, including any character the OCR is likely to confuse. The ground truth is the arbiter of `O/0`, `I/1`, `S/5`, `B/8`, `G/6`, `Z/2` disputes.

## Capture guidance

Shoot on the **TC22**, not a phone. Device optics, working distance, and in-store lighting all shift the input distribution, and results from another camera will not transfer.
