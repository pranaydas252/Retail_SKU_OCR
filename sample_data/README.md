# Sample Data

Real D-Mart SKU label images and their ground truth. **Image binaries are
git-ignored** — only this file, the folder structure, and the ground-truth JSON
are tracked.

## Approach: zero-shot

PaddleOCR is used **zero-shot**. No fine-tuning, no training data, no curated
public datasets (`CLAUDE.md` section 25).

PP-OCRv5 is already a strong general recognizer. The variable in this project is
the extraction and normalization logic layered on top of it, not the OCR model.
So this directory holds one thing: real label images shot on the TC22, plus
their transcribed ground truth.

## The two corpora

| File | Images | What it is |
| --- | --- | --- |
| `roi_labels.json` | 15, in `roi/` | **The one that counts.** ROI crops taken through the app itself, which is exactly what the backend receives. |
| `real_labels.json` | 9, in `real/` | Full-frame photographs, from before the app existed. Harder and unrepresentative — the app crops to the ROI window before uploading. Kept because the transcription is hand work that cannot be regenerated cheaply. |

Both were originally kept under `images/`, which is the **runtime scan output
directory**: git-ignored, written to by every scan, and safe to delete. An
evaluation corpus living there is one cleanup away from being lost, so it lives
here instead.

Run the accuracy gate against either:

```bat
python scripts\measure_accuracy.py
python scripts\measure_accuracy.py --truth sample_data\real_labels.json
```

## Ground-truth conventions

Both files use the same rules, and they are what makes the numbers mean
anything:

- A **value** means the pack prints it, recorded in the normalized form the
  server should produce — `YYYY-MM` for month/year, `YYYY-MM-DD` for full dates,
  plain decimal for money.
- **`null`** means the field is genuinely absent from the pack. A `null` the
  extractor "finds" is a **false accept**, the most serious failure class in this
  project. A wrong expiry date is worse than a recapture request.
- An **omitted key** means the capture is not legible enough to assert a truth.
  Not scored either way, so a guess is neither rewarded nor punished.
- Transcribe exactly what is printed, including any character the OCR is likely
  to confuse. The ground truth is the arbiter of `O/0`, `I/1`, `S/5`, `B/8`,
  `G/6`, `Z/2` disputes.

## Buckets

The condition buckets from `CLAUDE.md` section 25 — `good/`, `low_light/`,
`blur/`, `glare/`, `curved_packaging/`, `rotated/` — are still here and still
empty. Per-bucket accuracy is the right way to report, and a pipeline that
scores well on `good/` and collapses on `glare/` is not ready.

They stayed empty because the first real captures arrived as one undifferentiated
set, and 15 images do not divide into six buckets usefully. Sorting them is worth
doing when the corpus grows; doing it now would produce buckets of two.

## Capture guidance

Shoot on the **TC22**, not a phone, through the app's own sample mode. Device
optics, working distance, and in-store lighting all shift the input
distribution, and results from another camera will not transfer.
