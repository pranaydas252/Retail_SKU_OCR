"""PaddleOCR adapter.

This is the ONLY module permitted to import paddleocr. Everything downstream
consumes ``OcrToken``, so a PaddleOCR upgrade is contained to this file.

That containment is not theoretical. The installed 3.x line renamed and
restructured the 2.x API that nearly every published example still uses:

    2.x                      3.4.1
    -----------------------  ---------------------------------------------
    use_angle_cls            use_textline_orientation
    .ocr(img, cls=True)      .predict(img)
    [[bbox, (text, score)]]  result[0]['rec_texts' | 'rec_scores' | ...]

Verified against paddleocr 3.4.1 / paddlepaddle 3.3.1 on Windows CPU.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace

import cv2
import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Margin added around a detection box before re-reading it. Detection hugs
#: the ink, and the recogniser reads a line better with a little quiet space.
_CROP_PAD = 0.06

#: Crops smaller than this in either axis carry no readable text.
_MIN_CROP_PX = 8


@dataclass(frozen=True)
class OcrToken:
    """One recognized text region, with spatial data preserved.

    bbox is axis-aligned (x, y, width, height) in pixels of the image that was
    passed to OCR. Field extraction needs position to associate a printed
    label with its value, so this must never be collapsed to plain text
    (CLAUDE.md section 6).
    """

    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    variant: str | None = None

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class OcrService:
    """Lazily-initialized PaddleOCR wrapper.

    Model construction takes ~15s and is not thread-safe, so it happens once
    behind a lock. Call ``warmup()`` at startup to move that cost off the
    first real scan.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine = None
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._engine is not None

    def _build_engine(self):
        from paddleocr import PaddleOCR  # imported late: ~15s and very chatty

        s = self._settings

        # `lang` and `ocr_version` are deliberately NOT passed. PaddleOCR
        # ignores both when explicit model names are given and warns about it;
        # passing them would imply a control that does not exist.
        return PaddleOCR(
            text_detection_model_name=s.ocr_det_model_name,
            text_recognition_model_name=s.ocr_rec_model_name,
            use_doc_orientation_classify=s.ocr_use_doc_orientation_classify,
            use_doc_unwarping=s.ocr_use_doc_unwarping,
            use_textline_orientation=s.ocr_use_textline_orientation,
            text_det_limit_side_len=s.ocr_det_limit_side_len,
            text_det_limit_type=s.ocr_det_limit_type,
            text_det_thresh=s.ocr_det_thresh,
            text_det_box_thresh=s.ocr_det_box_thresh,
            text_rec_score_thresh=s.ocr_rec_score_thresh,
            text_recognition_batch_size=s.ocr_rec_batch_size,
            # PaddlePaddle 3.3.1 on Windows CPU raises
            #   NotImplementedError: (Unimplemented)
            #   ConvertPirAttribute2RuntimeAttribute not support
            #   [pir::ArrayAttribute<pir::DoubleAttribute>]
            # from its oneDNN backend during text detection. Disabling oneDNN
            # is the workaround; see config.ocr_enable_mkldnn.
            enable_mkldnn=s.ocr_enable_mkldnn,
        )

    def _ensure_engine(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    logger.info(
                        "Initializing PaddleOCR",
                        extra={
                            "detectionModel": self._settings.ocr_det_model_name,
                            "recognitionModel": self._settings.ocr_rec_model_name,
                            "mkldnn": self._settings.ocr_enable_mkldnn,
                        },
                    )
                    self._engine = self._build_engine()
        return self._engine

    def warmup(self) -> None:
        """Build models and run one throwaway inference.

        PaddleOCR downloads model files on first use and defers part of its
        setup to the first predict() call. Doing both here keeps the first
        real scan from paying for it.
        """
        engine = self._ensure_engine()
        blank = np.full((320, 640, 3), 255, dtype=np.uint8)
        engine.predict(blank)
        logger.info("OCR warmup complete")

    # -- inference --------------------------------------------------------

    def recognize(self, image: np.ndarray, variant: str | None = None) -> list[OcrToken]:
        """Run OCR and return tokens ordered top-to-bottom, left-to-right.

        Returns an empty list when nothing is detected. That is a valid
        outcome, not an error (CLAUDE.md section 22).
        """
        engine = self._ensure_engine()
        results = engine.predict(image)

        if not results:
            return []

        tokens = self._to_tokens(results[0], variant)

        if self._settings.ocr_retry_rotated_boxes:
            tokens = [self._rescue_rotated(image, t) for t in tokens]

        return tokens

    # -- rotated text rescue ----------------------------------------------

    def _rescue_rotated(self, image: np.ndarray, token: OcrToken) -> OcrToken:
        """Re-read a tall, low-confidence box after rotating it upright.

        Detection handles vertical text correctly; recognition reads it
        sideways and returns near-garbage at low confidence. Cropping the
        detected box and rotating it 90 degrees recovers the line.

        The bounding box is deliberately left untouched. Field extraction
        associates a label with its value by position (section 9), so the
        geometry must keep describing where the text sits on the pack, not
        where it sat inside a temporary rotated crop.

        Returns the original token unchanged when the rotation does not
        clearly beat it.
        """
        s = self._settings
        if token.height < token.width * s.ocr_rotated_aspect_ratio:
            return token
        if token.confidence >= s.ocr_rotated_max_confidence:
            return token

        crop = self._padded_crop(image, token)
        if crop is None:
            return token

        best_text, best_confidence = token.text, token.confidence
        for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
            text, confidence = self._read_plain(cv2.rotate(crop, rotation))
            if text and confidence > best_confidence:
                best_text, best_confidence = text, confidence

        if best_confidence < token.confidence + s.ocr_rotated_min_gain:
            return token

        logger.debug(
            "Rotated box re-read",
            extra={
                "before": token.text,
                "beforeConfidence": round(token.confidence, 3),
                "after": best_text,
                "afterConfidence": round(best_confidence, 3),
            },
        )
        return replace(token, text=best_text, confidence=best_confidence)

    @staticmethod
    def _padded_crop(image: np.ndarray, token: OcrToken) -> np.ndarray | None:
        """The token's region plus a small margin, or None if unusable.

        The margin matters: a detection box hugs the ink, and the recogniser
        reads a line noticeably better with a little quiet space around it.
        """
        pad_x = int(token.width * _CROP_PAD)
        pad_y = int(token.height * _CROP_PAD)

        x1 = max(0, token.x - pad_x)
        y1 = max(0, token.y - pad_y)
        x2 = min(image.shape[1], token.right + pad_x)
        y2 = min(image.shape[0], token.bottom + pad_y)

        if x2 - x1 < _MIN_CROP_PX or y2 - y1 < _MIN_CROP_PX:
            return None
        return image[y1:y2, x1:x2]

    def _read_plain(self, image: np.ndarray) -> tuple[str, float]:
        """Recognise an image and flatten it to one string and one score.

        Runs the engine directly rather than through recognize(), which would
        re-enter the rotation rescue and recurse.
        """
        try:
            results = self._ensure_engine().predict(image)
        except Exception:  # a rescue attempt must never fail the scan
            logger.warning("Rotated re-read failed", exc_info=True)
            return "", 0.0

        if not results:
            return "", 0.0

        tokens = self._to_tokens(results[0], None)
        texts = [t.text for t in tokens if t.text.strip()]
        if not texts:
            return "", 0.0

        scores = [t.confidence for t in tokens if t.text.strip()]
        return " ".join(texts), sum(scores) / len(scores)

    @staticmethod
    def _to_tokens(result, variant: str | None) -> list[OcrToken]:
        """Convert one PaddleOCR 3.x result into OcrToken objects.

        The result behaves like a mapping with these relevant keys:
            rec_texts   list[str]
            rec_scores  list[float]
            rec_boxes   ndarray (N, 4) of axis-aligned [x1, y1, x2, y2]
            rec_polys   list of (4, 2) corner arrays

        rec_boxes is preferred. rec_polys is the fallback because a rotated
        line has no axis-aligned box until we compute its extent.
        """
        texts = list(result.get("rec_texts") or [])
        scores = list(result.get("rec_scores") or [])
        boxes = result.get("rec_boxes")
        polys = result.get("rec_polys")

        if not texts:
            return []

        tokens: list[OcrToken] = []
        for i, text in enumerate(texts):
            confidence = float(scores[i]) if i < len(scores) else 0.0
            bounds = OcrService._bounds_for(i, boxes, polys)
            if bounds is None:
                logger.warning(
                    "OCR token without geometry, skipping",
                    extra={"tokenIndex": i, "text": text[:64]},
                )
                continue

            x1, y1, x2, y2 = bounds
            tokens.append(
                OcrToken(
                    text=str(text),
                    x=x1,
                    y=y1,
                    width=max(0, x2 - x1),
                    height=max(0, y2 - y1),
                    confidence=confidence,
                    variant=variant,
                )
            )

        # Reading order. The y-bucket keeps tokens printed on the same line
        # together even when their tops differ by a few pixels, which matters
        # for the same-line association rule of section 9.
        tokens.sort(key=lambda t: (round(t.y / 10), t.x))
        return tokens

    @staticmethod
    def _bounds_for(index: int, boxes, polys) -> tuple[int, int, int, int] | None:
        if boxes is not None and len(boxes) > index:
            box = np.asarray(boxes[index]).flatten()
            if box.size >= 4:
                x1, y1, x2, y2 = (int(v) for v in box[:4])
                return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

        if polys is not None and len(polys) > index:
            poly = np.asarray(polys[index]).reshape(-1, 2)
            if poly.size:
                xs, ys = poly[:, 0], poly[:, 1]
                return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())

        return None


_service: OcrService | None = None


def get_ocr_service() -> OcrService:
    """Process-wide OCR service. Models are expensive; share one instance."""
    global _service
    if _service is None:
        _service = OcrService()
    return _service
