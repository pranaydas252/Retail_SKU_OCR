# Sample Data

Test images for the OCR pipeline. **Image binaries are git-ignored** — only this file, the folder structure, and `expected.json` ground truth are tracked.

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

Use `null` for a field genuinely absent from the label. Do not guess. A field marked `null` that the extractor "finds" is a **false accept** — the most serious failure class in this project.

---

## Priority: real D-Mart labels

Public datasets are a scaffold, not a substitute. The POC accuracy gate is only meaningful against **real D-Mart SKU labels shot on the TC22 camera**, because device optics, working distance, and in-store lighting all shift the input distribution.

Public data is for building and smoke-testing the pipeline before those images exist.

---

## Public datasets

Ranked by usefulness to this project.

### 1. ExpDate — strongest match

<https://felizang.github.io/expdate/>

1,767 **real** product photographs (food, beverage, medicine) with near-horizontal dates, plus ~12,000 synthetic images.

Annotations: bounding box, transcription, image dimensions, and four classes — `date`, `due`, `prod`, `code`. Those classes map almost directly onto our fields: `prod` → manufacturing date, `due` → expiry date, `code` → batch/lot.

Download: Google Drive link on the project page. License is not stated — verify before any redistribution or commercial use.

**This is the best available proxy for our task.** Start here.

### 2. Retail Food Package Expiry Date Dataset — Kaggle

<https://www.kaggle.com/datasets/ziya07/retail-food-package-expiry-date-dataset>

Retail food packaging with expiry dates. Contents and license need manual verification — the Kaggle page is JavaScript-rendered and could not be read programmatically.

### 3. Expiry Date Digits — Kaggle

<https://www.kaggle.com/datasets/tareqkhan/expiry-date-digits>

Cropped digit images for expiry-date recognition. Useful for testing the **normalizer** and the `O/0 I/1 S/5 B/8 G/6 Z/2` confusion handling of `CLAUDE.md` section 10, not for end-to-end label extraction.

### 4. OCR Receipts Text Detection — Kaggle

<https://www.kaggle.com/datasets/trainingdatapro/ocr-receipts-text-detection>

Retail receipts with bounding-box annotations. Wrong medium (paper receipts, not product packaging), but useful for exercising the **spatial association** logic of section 9 — receipts have the same label-then-value layout problem.

### 5. Supermarket Shelves — Kaggle

<https://www.kaggle.com/datasets/humansintheloop/supermarket-shelves-dataset>

Shelf photographs with product and price annotations. Useful for negative testing — what the pipeline does when handed an image that is not a close-up label.

### 6. OCR Barcodes Detection — Kaggle

<https://www.kaggle.com/datasets/trainingdatapro/ocr-barcodes-detection>

Relevant later if barcode/GTIN matching against `SkuMaster` (section 15) is added.

### Not useful for OCR

These appear in searches for Indian retail data but are **tabular, not image**, datasets — no packaging photographs:

- Flipkart Retail Product Dataset
- Indian Retail Product Categorization Dataset
- Indian Retail Sales

They could seed `SkuMaster` if D-Mart master data is unavailable, nothing more.

---

---

## Merging ExpDate + Expiry Date Digits

Yes — but not as one image pool. They sit at **different pipeline layers**, and blending them produces a meaningless accuracy number.

### Why they don't merge directly

| | ExpDate | Expiry Date Digits |
| --- | --- | --- |
| Granularity | Whole product photograph | Cropped single digits |
| Annotation | bbox + transcription + class | Digit label |
| Exercises | det → rec → association → normalize | rec + normalize only |

Dropping digit crops into `sample_data/good/` would score them through a detection stage they were never meant to test, and would drag the reported end-to-end number toward whichever set is larger.

### How to merge them properly

Keep each source in its own directory under `datasets/` (git-ignored, licences unmixed). Write a **per-source adapter** that emits the common ground-truth schema, and have one harness consume both while **reporting separately**.

```text
datasets/expdate/          ── adapter ──┐
datasets/expiry-digits/    ── adapter ──┤──> common schema ──> accuracy harness
sample_data/<bucket>/      ── native ───┘                          │
                                                                   v
                              report broken down by source AND bucket
```

Two test layers:

- **Layer 1 — end-to-end.** ExpDate *real* photos (1,767). Full pipeline.
- **Layer 2 — unit.** Digit crops. Feeds recognizer and normalizer tests directly, especially the `O/0 I/1 S/5 B/8 G/6 Z/2` confusion handling of `CLAUDE.md` section 10.

### ExpDate class mapping

| ExpDate class | Our field |
| --- | --- |
| `prod` | `manufacturingDate` |
| `due` | `expiryDate` |
| `date` | ambiguous — needs a disambiguation rule, do not map blindly |
| `code` | `batchNumber` **or** `lotCode` — ExpDate does not distinguish them |

### Hard limits — read before trusting the numbers

- **Exclude the synthetic split from the accuracy gate.** ExpDate ships ~12,000 synthetic images plus ~128,000 synthetic date crops and ~450,000 component crops. Useful for stress-testing the normalizer, ruinous if mixed into a reported accuracy figure. Real-only for the gate.
- **MRP has zero public coverage.** One of our five required fields cannot be validated by any dataset here.
- **Batch vs lot cannot be validated either** — `code` collapses both.
- **Domain gap.** ExpDate is dominated by inkjet/dot-matrix date stamps on food and medicine packaging. D-Mart SKU labels are printed label stock. Different print process, different failure modes.

Net: this combination validates roughly **two and a half of five fields** and tests the date logic well. It does not predict field performance on D-Mart labels. Real images remain the gate.

---

## Downloading

Kaggle datasets need an account and API token (`~/.kaggle/kaggle.json`):

```powershell
pip install kaggle
kaggle datasets download -d ziya07/retail-food-package-expiry-date-dataset -p datasets/
```

`datasets/` is git-ignored. Extract, review, then copy the useful images into the bucket folders above and write matching `expected.json` entries.

**Check each dataset's license before use.** Several list no license at all, which means no granted rights — acceptable for local R&D evaluation, not for redistribution.
