"""Image validation and preprocessing tests. No OCR, no network."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.config import get_settings
from app.services import image_service
from app.services.image_service import ImageValidationError


def encode(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    return buffer.tobytes()


def blank(width: int = 640, height: int = 480) -> np.ndarray:
    return np.full((height, width, 3), 255, dtype=np.uint8)


class TestDecodeImage:
    def test_decodes_valid_jpeg(self):
        image = image_service.decode_image(encode(blank()))
        assert image.shape[:2] == (480, 640)

    def test_rejects_empty_upload(self):
        with pytest.raises(ImageValidationError) as exc:
            image_service.decode_image(b"")
        assert exc.value.code == "EMPTY_UPLOAD"

    def test_rejects_non_image_bytes(self):
        with pytest.raises(ImageValidationError) as exc:
            image_service.decode_image(b"this is not an image at all")
        assert exc.value.code == "UNREADABLE_IMAGE"

    def test_rejects_undersized_image(self):
        with pytest.raises(ImageValidationError) as exc:
            image_service.decode_image(encode(blank(64, 64)))
        assert exc.value.code == "IMAGE_TOO_SMALL"

    def test_rejects_oversized_upload(self):
        settings = get_settings().model_copy(update={"max_upload_bytes": 100})
        with pytest.raises(ImageValidationError) as exc:
            image_service.decode_image(encode(blank()), settings)
        assert exc.value.code == "IMAGE_TOO_LARGE"


class TestVariants:
    @pytest.mark.parametrize("variant", ["original", "enhanced", "threshold"])
    def test_variant_preserves_dimensions_and_channels(self, variant):
        source = blank()
        cv2.putText(source, "BATCH A23C91", (20, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)

        result = image_service.build_variant(source, variant)

        assert result.shape == source.shape, "OCR expects 3-channel input"
        assert result.dtype == np.uint8

    def test_original_is_untouched(self):
        source = blank()
        assert np.array_equal(image_service.build_variant(source, "original"), source)

    def test_threshold_is_binary(self):
        source = blank()
        cv2.putText(source, "MFG 07/26", (20, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)

        result = image_service.build_variant(source, "threshold")

        assert set(np.unique(result)).issubset({0, 255})

    def test_unknown_variant_raises(self):
        with pytest.raises(ValueError, match="Unknown preprocessing variant"):
            image_service.build_variant(blank(), "sharpen_twice")


class TestResize:
    def test_downscales_to_max_side(self):
        result = image_service.resize_for_ocr(blank(2000, 1000), 960)
        assert max(result.shape[:2]) == 960

    def test_preserves_aspect_ratio(self):
        result = image_service.resize_for_ocr(blank(2000, 1000), 960)
        height, width = result.shape[:2]
        assert abs((width / height) - 2.0) < 0.02

    def test_never_upscales(self):
        source = blank(320, 240)
        assert image_service.resize_for_ocr(source, 960).shape == source.shape


class TestQuality:
    def test_blank_image_has_near_zero_blur_variance(self):
        assert image_service.image_quality(blank())["blurVariance"] < 1.0

    def test_detailed_image_has_higher_blur_variance(self):
        detailed = blank()
        cv2.putText(detailed, "BATCH NO A23C91", (20, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)

        flat = image_service.image_quality(blank())["blurVariance"]
        sharp = image_service.image_quality(detailed)["blurVariance"]

        assert sharp > flat

    def test_dark_image_reports_low_brightness(self):
        dark = np.full((480, 640, 3), 12, dtype=np.uint8)
        assert image_service.image_quality(dark)["meanBrightness"] < 20
