"""Vision-language model adapter.

This is the ONLY module permitted to talk to the VLM runtime, for the same
reason `ocr_service` is the only one permitted to import paddleocr: everything
downstream consumes ``OcrToken``, so swapping the model or the runtime is
contained to this file.

## Why there is a second engine at all

PP-OCRv5 cannot read some of the print this project has to handle. Measured on
a TC22 capture of an Eno carton, the pack's printed field names came back at
0.95-0.99 confidence while the inkjet values beside them came back at 0.05-0.53
as "Phm", "Voow", "JAND". The values were 218-291px tall in the original, so it
is not a resolution problem, and three PP-OCRv5 recognition models were tried
without success. No extraction rule recovers text that was never recognised.

## What this is not

It is not a replacement for PP-OCRv5. Measured earlier on the previous corpus,
`PaddleOCR-VL-1.5-GGUF` reached 36% on the core fields against PP-OCRv5's 43%,
because a vision-language model reads the whole image at a fixed token budget
while PP-OCRv5 detects regions and recognises each one at its own scale. The
intended use is as a second opinion whose agreement carries information — an
earlier ensemble measurement had 23 of 50 values answered by exactly one of the
two engines, and agreement-only merging gave 2 wrong values against 14.

## Output shape

The model returns JSON. Its values are then fed through the SAME extraction,
normalization and validation path as PP-OCRv5 output, by synthesizing one token
per field. That is deliberate: a number produced by a separate scoring path
would not be comparable to the PP-OCRv5 figures, and comparability is the whole
reason for running two engines.

The synthesized tokens carry no real geometry. Spatial association has nothing
to work with here, which is exactly why the prompt asks the model to do the
label-to-value matching itself.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field as dataclass_field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from app.config import Settings, get_settings
from app.services.ocr_service import OcrToken

logger = logging.getLogger(__name__)


class VlmError(RuntimeError):
    """The model could not be reached or returned nothing usable."""


@dataclass
class VlmResult:
    """One model call."""

    #: Field name to value, exactly as printed. Never normalized here.
    values: dict[str, str] = dataclass_field(default_factory=dict)
    raw: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.values)


#: The model speaks the pack's vocabulary; the rest of the system speaks the
#: API's. Mapping here keeps the prompt readable and the wire contract stable.
_FIELD_NAMES = {
    "batch": "batchNumber",
    "lot": "lotCode",
    "mfd": "manufacturingDate",
    "mfg": "manufacturingDate",
    "exp": "expiryDate",
    "mrp": "mrp",
}

#: Field labels prepended to synthesized tokens, so the existing inline
#: association ("MFG 07/26") does the work and no new extraction path is needed.
_TOKEN_LABELS = {
    "batchNumber": "BATCH NO",
    "lotCode": "LOT NO",
    "manufacturingDate": "MFG",
    "expiryDate": "EXP",
    "mrp": "MRP",
}

#: Models wrap JSON in prose or fences however they please.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@lru_cache
def load_prompt(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def encode_image(image: np.ndarray, max_side: int) -> str:
    """Downscale to the model's budget and encode as base64 JPEG.

    Never upscales: inventing pixels does not add detail and costs tokens.
    Quality 92 for the same reason the app uploads at 92 — label text is small
    and JPEG ringing around thin strokes is exactly what a recogniser trips on.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > max_side:
        scale = max_side / longest
        image = cv2.resize(
            image,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_AREA,
        )

    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise VlmError("Could not encode the image for the model")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def parse_values(raw: str) -> dict[str, str]:
    """Pull the field map out of whatever the model wrapped it in.

    Returns an empty dict rather than raising. A model that answers with prose
    is a bad answer, not a server error — the scan still has PP-OCRv5's result.
    """
    block = _JSON_BLOCK.search(raw or "")
    if not block:
        return {}

    try:
        parsed = json.loads(block.group(0))
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    values: dict[str, str] = {}
    for key, value in parsed.items():
        name = _FIELD_NAMES.get(str(key).strip().lower())
        if not name or value is None:
            continue
        text = str(value).strip()
        # A model asked to omit unknown fields will still sometimes answer with
        # a placeholder rather than leaving the key out.
        if not text or text.lower() in {"null", "none", "n/a", "-", "unknown"}:
            continue
        values.setdefault(name, text)

    return values


def transcript_to_tokens(raw: str) -> list[OcrToken]:
    """Turn a transcription into tokens, one per printed line.

    Used for models that read the image aloud rather than reporting fields —
    which, measured, is what PaddleOCR-VL does regardless of what the prompt
    asks for. Feeding the text through our own extractor rather than trusting
    the model to identify fields keeps one extraction path, so the resulting
    accuracy is directly comparable to PP-OCRv5's.

    Geometry is synthesized as a single column in reading order. That is enough
    for the inline and same-line association a transcript can support, and no
    more than the transcript actually contains.
    """
    tokens: list[OcrToken] = []
    y = 0
    seen: set[str] = set()

    for line in (raw or "").splitlines():
        text = line.strip().lstrip("#*-• ").strip()
        if not text:
            continue
        # Repeat loops are the failure mode of these models on dense label
        # text; a line repeated verbatim carries nothing and would let one
        # misread outvote everything else in extraction.
        if text in seen:
            continue
        seen.add(text)

        tokens.append(
            OcrToken(
                text=text,
                x=0,
                y=y,
                width=max(len(text) * 12, 40),
                height=30,
                confidence=0.0,
                variant="vlm",
            )
        )
        y += 45

    return tokens


def to_tokens(values: dict[str, str]) -> list[OcrToken]:
    """Synthesize tokens so VLM output takes the same path as PP-OCRv5 output.

    One token per field, laid out as a single column of plausible printed rows.
    The geometry is fabricated and says so: there is no spatial information in
    a JSON answer. Each token carries its own field label inline, which is what
    lets the existing extractor associate it without a new code path.

    Confidence is 0.0 because the runtime does not report one. Downstream
    scoring must not read this as "certainly wrong" — see confidence.py, which
    treats a VLM candidate on its own terms.
    """
    tokens: list[OcrToken] = []
    y = 0
    for name, value in values.items():
        label = _TOKEN_LABELS.get(name)
        if not label:
            continue
        text = f"{label} {value}"
        tokens.append(
            OcrToken(
                text=text,
                x=0,
                y=y,
                width=max(len(text) * 12, 40),
                height=30,
                confidence=0.0,
                variant="vlm",
            )
        )
        y += 45
    return tokens


class VlmService:
    """Talks to an Ollama-compatible runtime over HTTP.

    Deliberately not a persistent client. The model is called at most once per
    scan and the runtime is a local process; a pooled connection would buy
    nothing and would have to be torn down correctly on reload.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def model(self) -> str:
        return self._settings.vlm_model

    def is_available(self) -> bool:
        """Whether the runtime answers and has the configured model."""
        import httpx

        s = self._settings
        try:
            response = httpx.get(f"{s.vlm_base_url}/api/tags", timeout=5)
            response.raise_for_status()
            names = {m.get("name", "") for m in response.json().get("models", [])}
        except Exception as exc:
            logger.warning("VLM runtime unreachable", extra={"error": str(exc)})
            return False

        if s.vlm_model not in names:
            logger.warning(
                "VLM model not installed",
                extra={"model": s.vlm_model, "available": sorted(names)},
            )
            return False
        return True

    def read(self, image: np.ndarray) -> VlmResult:
        """Run the model over one image and return the fields it reports."""
        import httpx

        s = self._settings
        payload = {
            "model": s.vlm_model,
            "prompt": load_prompt(str(s.vlm_prompt)),
            "images": [encode_image(image, s.vlm_max_side)],
            "stream": False,
            "options": {
                "temperature": s.vlm_temperature,
                # Both measured. Without them the model falls into repeat loops
                # on dense label text and spends 25s producing "1.00 2.00
                # 3.00..." or LaTeX.
                "num_predict": s.vlm_max_tokens,
                "repeat_penalty": s.vlm_repeat_penalty,
            },
        }

        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{s.vlm_base_url}/api/generate",
                json=payload,
                timeout=s.vlm_timeout_seconds,
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
        except Exception as exc:
            raise VlmError(f"VLM request failed: {exc}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        values = parse_values(raw) if s.vlm_mode == "json" else {}

        logger.info(
            "VLM read complete",
            extra={
                "model": s.vlm_model,
                "mode": s.vlm_mode,
                "vlmMs": duration_ms,
                "fieldsReported": sorted(values),
                "characters": len(raw),
            },
        )
        if s.vlm_mode == "json" and not values:
            logger.warning("VLM returned no usable JSON", extra={"raw": raw[:300]})

        return VlmResult(values=values, raw=raw, duration_ms=duration_ms)

    def tokens(self, image: np.ndarray) -> list[OcrToken]:
        """Read an image and return tokens for the normal extraction path."""
        result = self.read(image)
        if self._settings.vlm_mode == "json":
            return to_tokens(result.values)
        return transcript_to_tokens(result.raw)


_service: VlmService | None = None


def get_vlm_service() -> VlmService:
    """Process-wide VLM service."""
    global _service
    if _service is None:
        _service = VlmService()
    return _service
