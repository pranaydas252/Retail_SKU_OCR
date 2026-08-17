# Sample Data

Real Reliance Retail SKU label images and their ground truth. **Image binaries are
git-ignored** — only this file, the folder structure, and the ground-truth JSON
are tracked.

## Approach: zero-shot

PaddleOCR is used **zero-shot**. No fine-tuning, no training data, no curated
public datasets (`CLAUDE.md` section 25).

PP-OCRv5 is already a strong general recognizer. The variable in this project is
the extraction and normalization logic layered on top of it, not the OCR model.
So this directory holds one thing: real label images shot on the TC22, plus
their transcribed ground truth.

## Status: empty, awaiting a fresh collection

The previous corpus — 15 ROI crops and 9 full-frame photographs — was deleted
on 2026-08-16. It predated three changes that alter what the backend actually
receives:

- capture is now gated on a readiness score, so a frame nobody would have kept
  never reaches the server
- the crop is deskewed on-device before upload
- the readiness analysis runs at 1280x720 rather than 640x480

Measuring a recognition engine against captures taken before all three would
produce a number that does not describe production. The old ground-truth
transcriptions remain in git history if a comparison is ever wanted.

**Until this directory is refilled there is no accuracy gate.** That is the
accepted cost of replacing the corpus rather than mixing two distributions.

## Collecting a new one

Turn on **sample collection** in the app's settings dialog. Captures are then
saved to the device instead of being uploaded, and the camera stays put between
shots so a shelf can be worked through in one pass.

```bat
adb pull /sdcard/Android/data/com.markss.retailocr/files/Pictures/samples sample_data\roi
python scripts\scaffold_ground_truth.py
```

The scaffold writes `roi_labels.json` with one entry per image and the field
names already in place, so the transcription is filling in values rather than
typing structure. Then:

```bat
python scripts\measure_accuracy.py
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
