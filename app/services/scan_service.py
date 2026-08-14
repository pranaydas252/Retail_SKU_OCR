"""Scan orchestration.

Sequences validation, storage, preprocessing, and OCR, and assembles the API
response. Routes stay thin; this is where the pipeline order lives.

Phase 1 stops at OCR tokens. Field extraction, normalization, validation, and
confidence scoring arrive in Phase 2; persistence in Phase 3.
"""

from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.db.connection import next_scan_code
from app.models.response_models import (
    BoundingBox,
    OcrTokenModel,
    ProcessingStatus,
    ScanResponse,
)
from app.services import image_service
from app.services.ocr_service import OcrToken, get_ocr_service
from app.utils.logging import StageTimer

logger = logging.getLogger(__name__)


def process_scan(
    data: bytes,
    device_id: str | None = None,
    device_model: str | None = None,
    settings: Settings | None = None,
) -> ScanResponse:
    """Run one image through the pipeline.

    Raises ImageValidationError for unusable input (HTTP 400). Any other
    failure propagates and is handled as a server error by the route.
    """
    s = settings or get_settings()
    timer = StageTimer()
    scan_code = next_scan_code()

    log_context = {
        "scanId": scan_code,
        "deviceId": device_id,
        "deviceModel": device_model,
    }
    logger.info("Scan started", extra=log_context)

    with timer.stage("validateMs"):
        image = image_service.decode_image(data, s)
        quality = image_service.image_quality(image)

    with timer.stage("storeMs"):
        image_path = image_service.store_image(data, scan_code, s)

    ocr = get_ocr_service()
    tokens: list[OcrToken] = []
    variant_used: str | None = None

    # Variants are tried in configured order and the first that finds text
    # wins. This is a deliberate short-circuit: each variant costs a full OCR
    # pass, and cross-variant agreement scoring (section 12) belongs in
    # Phase 2 where there are fields to compare.
    with timer.stage("ocrMs"):
        for variant in s.variant_list:
            processed = image_service.build_variant(image, variant)
            processed = image_service.resize_for_ocr(processed, s.ocr_det_limit_side_len)
            image_service.store_variant(processed, scan_code, variant, s)

            tokens = ocr.recognize(processed, variant=variant)
            variant_used = variant
            if tokens:
                break

            logger.info(
                "Variant produced no text",
                extra={**log_context, "variant": variant},
            )

    timings = timer.as_dict()
    status = (
        ProcessingStatus.COMPLETED if tokens else ProcessingStatus.NO_TEXT_DETECTED
    )

    logger.info(
        "Scan finished",
        extra={
            **log_context,
            "status": status.value,
            "tokenCount": len(tokens),
            "variantUsed": variant_used,
            "imagePath": str(image_path),
            **quality,
            **timings,
        },
    )

    return ScanResponse(
        scanId=scan_code,
        status=status,
        overallConfidence=_mean_confidence(tokens),
        fields={},  # Phase 2
        tokens=[_to_model(t) for t in tokens],
        timings=timings,
        variantUsed=variant_used,
        message=(
            None
            if tokens
            else "No text was detected. Ask the operator to recapture the label."
        ),
    )


def _mean_confidence(tokens: list[OcrToken]) -> float | None:
    """Mean OCR confidence.

    This is a raw OCR average and NOT the application confidence score of
    CLAUDE.md section 12, which needs extracted fields to combine label-match,
    spatial, format, and cross-field signals. It is reported in Phase 1 only
    so the pipeline has something observable, and is replaced in Phase 2.
    """
    if not tokens:
        return None
    return round(sum(t.confidence for t in tokens) / len(tokens), 4)


def _to_model(token: OcrToken) -> OcrTokenModel:
    return OcrTokenModel(
        text=token.text,
        bbox=BoundingBox(
            x=token.x, y=token.y, width=token.width, height=token.height
        ),
        confidence=round(token.confidence, 4),
        variant=token.variant,
    )
