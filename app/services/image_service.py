"""Image validation, storage, and OpenCV preprocessing.

Preprocessing follows CLAUDE.md section 7. Each variant costs a full OCR pass
(~1.6s on the reference machine), so the enabled set is configuration, and the
default is ``original`` alone. Do not enable more without measuring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ImageValidationError(ValueError):
    """Raised when an upload cannot be used. Maps to HTTP 400."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def decode_image(data: bytes, settings: Settings | None = None) -> np.ndarray:
    """Decode uploaded bytes into a BGR array, or raise ImageValidationError."""
    s = settings or get_settings()

    if not data:
        raise ImageValidationError("EMPTY_UPLOAD", "No image data was received.")

    if len(data) > s.max_upload_bytes:
        raise ImageValidationError(
            "IMAGE_TOO_LARGE",
            f"Image exceeds the {s.max_upload_bytes // (1024 * 1024)}MB limit.",
        )

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageValidationError(
            "UNREADABLE_IMAGE", "The uploaded file is not a readable image."
        )

    height, width = image.shape[:2]
    if width < s.min_image_width or height < s.min_image_height:
        raise ImageValidationError(
            "IMAGE_TOO_SMALL",
            f"Image is {width}x{height}; minimum is "
            f"{s.min_image_width}x{s.min_image_height}.",
        )

    return image


def image_quality(image: np.ndarray) -> dict[str, float]:
    """Cheap quality signals, recorded for diagnosis.

    These are reported, not enforced. The server does not reject a blurry
    image — the operator decides after seeing the confidence bands. Rejecting
    here would hide failures the R&D phase needs to see.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        "blurVariance": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "meanBrightness": float(gray.mean()),
    }


def build_variant(image: np.ndarray, variant: str) -> np.ndarray:
    """Produce one preprocessing variant.

    original   untouched BGR, straight from the camera
    enhanced   grayscale, CLAHE contrast, mild sharpen — the general-purpose
               improvement for uneven in-store lighting
    threshold  Otsu binarization — helps crisp dark-on-light label print,
               destroys low-contrast or glare-affected print, so it is a
               comparison candidate rather than a default
    """
    if variant == "original":
        return image

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    if variant == "enhanced":
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        equalized = clahe.apply(gray)
        blurred = cv2.GaussianBlur(equalized, (0, 0), sigmaX=3)
        sharpened = cv2.addWeighted(equalized, 1.5, blurred, -0.5, 0)
        return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)

    if variant == "threshold":
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        equalized = clahe.apply(gray)
        _, binary = cv2.threshold(
            equalized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    raise ValueError(f"Unknown preprocessing variant: {variant!r}")


def resize_for_ocr(image: np.ndarray, max_side: int) -> np.ndarray:
    """Downscale so the longest side is at most max_side.

    Never upscales — inventing pixels does not add detail and costs OCR time.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image

    scale = max_side / longest
    return cv2.resize(
        image,
        (int(width * scale), int(height * scale)),
        interpolation=cv2.INTER_AREA,
    )


def store_image(
    data: bytes, scan_code: str, settings: Settings | None = None
) -> Path:
    """Write the original upload to disk and return its path.

    Layout is images/YYYY/MM/DD/SCAN-000001.jpg (CLAUDE.md section 20). Image
    binaries stay on the filesystem; SQL Server holds only this path.
    """
    s = settings or get_settings()
    now = datetime.now(timezone.utc)

    directory = Path(s.image_root) / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{scan_code}.jpg"
    path.write_bytes(data)
    return path


def store_variant(
    image: np.ndarray, scan_code: str, variant: str, settings: Settings | None = None
) -> Path | None:
    """Write a processed variant for debugging, if enabled."""
    s = settings or get_settings()
    if not s.save_processed_variants:
        return None

    directory = Path(s.debug_artifact_root) / scan_code
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{variant}.jpg"
    cv2.imwrite(str(path), image)
    return path
