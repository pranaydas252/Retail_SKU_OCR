"""Scan orchestration.

Sequences validation, storage, preprocessing, and OCR, and assembles the API
response. Routes stay thin; this is where the pipeline order lives.

Phase 1 stops at OCR tokens. Field extraction, normalization, validation, and
confidence scoring arrive in Phase 2; persistence in Phase 3.
"""

from __future__ import annotations

import logging
import re

from app.config import Settings, get_settings
from app.db import repositories
from app.db.connection import next_scan_code
from app.db.repositories import PersistenceError
from app.models.response_models import (
    BoundingBox,
    ConfirmScanResponse,
    ExtractedField,
    OcrTokenModel,
    ProcessingStatus,
    ScanResponse,
)
from app.services import (
    confidence,
    field_extractor,
    image_service,
    normalizer,
    validator,
)
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

    # Persist best-effort. A database outage must not throw away several
    # seconds of OCR the operator already waited for — the results are still
    # usable and the response says whether they were stored. The commit point
    # that genuinely requires the database is /confirm, which does fail hard.
    persisted = True
    try:
        repositories.save_scan(
            scan_id=repositories.new_scan_id(),
            scan_code=scan_code,
            status=status,
            fields=fields,
            tokens=tokens,
            image_path=str(image_path),
            overall_confidence=overall_confidence,
            variant_used=variant_used,
            device_id=device_id,
            device_model=device_model,
            processing_ms=timings.get("totalMs"),
        )
    except PersistenceError as exc:
        persisted = False
        logger.error(
            "Scan could not be persisted; results returned unsaved",
            extra={**log_context, "error": str(exc)},
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
            "persisted": persisted,
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
        persisted=persisted,
        message=(
            None
            if tokens
            else "No text was detected. Ask the operator to recapture the label."
        ),
    )


def confirm_scan(
    scan_code: str, confirmed: dict[str, str | None]
) -> ConfirmScanResponse:
    """Validate and persist the operator's confirmed values.

    The server re-validates rather than trusting the device (section 13). A
    validation problem does not block the commit: the operator is the final
    authority (section 4), and refusing their input would leave them unable to
    record a genuinely odd label. The problems are recorded against the fields
    so the audit trail shows what was accepted despite a warning.
    """
    specs = {spec.name: spec for spec in field_extractor.load_field_specs()}
    types = {name: spec.value_type for name, spec in specs.items()}

    normalized: dict[str, str | None] = {}
    notes: dict[str, list[str]] = {}

    for name, raw in confirmed.items():
        if raw is None or not str(raw).strip():
            normalized[name] = None
            continue

        spec = specs.get(name)
        if spec is None:
            # Unknown field names are kept verbatim rather than dropped; the
            # alias table is meant to grow, and silently discarding operator
            # input would be worse than storing something unrecognized.
            normalized[name] = str(raw).strip()
            notes.setdefault(name, []).append("unknown field, stored as entered")
            continue

        result = normalizer.normalize(str(raw), spec.value_type, spec.date_precision)
        if result.ok:
            normalized[name] = result.value
        else:
            # Keep what the operator typed. Losing their correction because it
            # failed a format rule would be the worst possible outcome.
            normalized[name] = str(raw).strip()
            notes.setdefault(name, []).extend(result.notes or ["could not normalize"])

    validation = validator.validate(normalized, types)
    for name, problems in validation.field_notes.items():
        if name not in normalized:
            continue
        # "not found" describes a failed extraction. When the operator
        # deliberately clears a field they are asserting it is absent from the
        # label, which is information rather than a problem — recording it as
        # one would pollute the audit trail on every legitimately short pack.
        if normalized[name] is None:
            problems = [p for p in problems if p != "not found"]
        if problems:
            notes.setdefault(name, []).extend(problems)

    flat_notes = {name: "; ".join(problems)[:500] for name, problems in notes.items()}

    scan_id = repositories.confirm_scan(scan_code, normalized, flat_notes)

    logger.info(
        "Scan confirmed",
        extra={
            "scanId": scan_code,
            "scanRowId": scan_id,
            "editedFields": sorted(normalized),
            "validationNotes": flat_notes,
        },
    )

    return ConfirmScanResponse(
        scanId=scan_code,
        status=ProcessingStatus.CONFIRMED,
        persisted=True,
        validationNotes=notes,
        qrPayload=build_qr_payload(scan_code, normalized),
    )


def build_qr_payload(scan_code: str, values: dict[str, str | None]) -> str:
    """Build the QR payload for the printed label (section 17).

    Pipe-delimited and uppercase so ZPL can encode it in alphanumeric mode,
    which packs two characters per 11 bits. Mixed case or symbols would force
    byte mode and roughly halve capacity against the 10mm budget.

    Empty fields keep their position so the payload stays positional and a
    scanner-side parser does not have to guess which field is missing.
    """
    ordered = [
        scan_code,
        values.get("batchNumber") or "",
        values.get("manufacturingDate") or "",
        values.get("expiryDate") or "",
        values.get("lotCode") or "",
        values.get("mrp") or "",
    ]
    return "|".join(part.upper() for part in ordered)


#: Curated labels for the fields the rule extractor knows about.
_CURATED_LABELS = {
    "batchNumber": "Batch number",
    "manufacturingDate": "Manufacturing date",
    "expiryDate": "Expiry date",
    "lotCode": "Lot code",
    "mrp": "MRP",
}


def display_name(key: str) -> str:
    """Human label for a field key.

    Every field carries one so the app can render a key it has never seen. The
    field set is deliberately not fixed: a document-understanding model may
    return whatever a given pack prints — ``netContents``, a licence number, a
    field nobody predicted — and shipping an Android string per key would make
    every new field an app release.

    Known keys get a curated label; anything else is humanised from the key,
    which is wrong far less often than showing a raw identifier.
    """
    if key in _CURATED_LABELS:
        return _CURATED_LABELS[key]

    # camelCase / snake_case / kebab-case -> "Net contents"
    spaced = re.sub(r"[_-]+", " ", key)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced).strip()
    if not spaced:
        return key
    return spaced[0].upper() + spaced[1:]


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
                displayName=display_name(name),
                expected=True,
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
            displayName=display_name(name),
            expected=True,
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
