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
    ExtractedField,
    OcrTokenModel,
    ProcessingStatus,
    ScanResponse,
)
from app.services import confidence, field_extractor, image_service, validator
from app.services.ocr_service import OcrToken, get_ocr_service
from app.utils.logging import StageTimer

logger = logging.getLogger(__name__)

# Fields that must be present for a scan to be considered fully successful.
# Overall confidence is the minimum across these, so a missing or uncertain
# expiry date cannot be averaged away by four good fields.
REQUIRED_FIELDS = [
    "batchNumber",
    "manufacturingDate",
    "expiryDate",
    "lotCode",
    "mrp",
]


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

    fields: dict[str, ExtractedField] = {}
    overall_confidence: float | None = None

    if tokens:
        with timer.stage("extractMs"):
            fields, overall_confidence = extract_and_score(tokens)

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
            "overallConfidence": overall_confidence,
            "fieldsFound": [k for k, v in fields.items() if v.value is not None],
            "fieldsMissing": [k for k, v in fields.items() if v.value is None],
            **quality,
            **timings,
        },
    )

    return ScanResponse(
        scanId=scan_code,
        status=status,
        overallConfidence=overall_confidence,
        fields=fields,
        tokens=[_to_model(t) for t in tokens],
        timings=timings,
        variantUsed=variant_used,
        message=(
            None
            if tokens
            else "No text was detected. Ask the operator to recapture the label."
        ),
    )


def extract_and_score(
    tokens: list[OcrToken],
) -> tuple[dict[str, ExtractedField], float | None]:
    """Extract, validate, and score fields from OCR tokens.

    Every required field appears in the result, including ones that were not
    found — those carry a null value so the Android UI can render an explicit
    empty box for the operator rather than silently omitting the field
    (CLAUDE.md section 11).
    """
    candidates = field_extractor.extract_fields(tokens)
    specs = {spec.name: spec for spec in field_extractor.load_field_specs()}

    values = {
        name: (candidates[name].normalized.value if name in candidates else None)
        for name in specs
    }
    types = {name: spec.value_type for name, spec in specs.items()}

    validation = validator.validate(values, types)

    fields: dict[str, ExtractedField] = {}
    scores: dict[str, float] = {}
    found: set[str] = set()

    for name in specs:
        candidate = candidates.get(name)

        if candidate is None or not candidate.normalized.ok:
            fields[name] = ExtractedField(
                value=None,
                confidence=0.0,
                band=confidence.band(0.0),
                source="NOT_FOUND",
            )
            scores[name] = 0.0
            continue

        score = confidence.score_field(
            candidate,
            format_ok=validation.is_valid(name),
            consistency_ok=validation.consistency_ok,
        )
        scores[name] = score
        found.add(name)

        fields[name] = ExtractedField(
            value=candidate.normalized.value,
            confidence=score,
            band=confidence.band(score),
            source="DERIVED_RULE" if candidate.strategy == "derived" else "OCR_RULES",
            rawValue=candidate.raw_value,
        )

    return fields, confidence.overall(scores, found)


def _to_model(token: OcrToken) -> OcrTokenModel:
    return OcrTokenModel(
        text=token.text,
        bbox=BoundingBox(
            x=token.x, y=token.y, width=token.width, height=token.height
        ),
        confidence=round(token.confidence, 4),
        variant=token.variant,
    )
