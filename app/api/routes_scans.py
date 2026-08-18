"""Scan endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.security import require_api_key
from app.db import repositories
from app.db.repositories import PersistenceError
from app.models.request_models import ConfirmScanRequest
from app.models.response_models import (
    ConfirmScanResponse,
    PrintScanResponse,
    ProcessingStatus,
    ScanResponse,
    StoredScanResponse,
)
from app.services import scan_service
from app.services.image_service import ImageValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scans"], dependencies=[Depends(require_api_key)])


@router.post(
    "/scans",
    response_model=ScanResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
def create_scan(
    image: UploadFile = File(..., description="JPEG or PNG of the SKU label"),
    deviceId: str | None = Form(default=None),
    deviceModel: str | None = Form(default=None),
    engineMode: str | None = Form(
        default=None,
        description='"both" or "vlm"; omitted uses the server default',
    ),
) -> ScanResponse:
    """Upload a label image and receive OCR results.

    Returns 200 with status NO_TEXT_DETECTED when the image is valid but holds
    no readable text — that is an operator recapture prompt, not a server
    failure (CLAUDE.md section 22).

    Declared ``def`` rather than ``async def`` deliberately. OCR is several
    seconds of blocking CPU work; on the event loop it would stall every other
    request, including /health. FastAPI runs sync endpoints in a threadpool,
    which keeps the server responsive while a scan is in flight.
    """
    data = image.file.read()

    try:
        return scan_service.process_scan(
            data, device_id=deviceId, device_model=deviceModel,
            engine_mode=engineMode
        )
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"status": "ERROR", "code": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        # Log the detail, return a safe body. Internals never reach the device.
        logger.exception("Scan processing failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "ERROR",
                "code": "OCR_PROCESSING_FAILED",
                "message": "The image could not be processed. Please try again.",
            },
        ) from exc


@router.post(
    "/scans/{scan_code}/confirm",
    response_model=ConfirmScanResponse,
    response_model_by_alias=True,
)
def confirm_scan(scan_code: str, body: ConfirmScanRequest) -> ConfirmScanResponse:
    """Persist the operator-confirmed values for a scan.

    This is the commit point, so unlike the scan upload a database failure is
    fatal here — reporting success without storing the operator's confirmation
    would be the worst outcome available (CLAUDE.md section 22).
    """
    try:
        return scan_service.confirm_scan(scan_code, body.fields)
    except PersistenceError as exc:
        message = str(exc)
        if "Unknown scan" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "status": "ERROR",
                    "code": "SCAN_NOT_FOUND",
                    "message": f"No scan found with id {scan_code}.",
                },
            ) from exc

        logger.exception("Confirm failed", extra={"scanId": scan_code})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "ERROR",
                "code": "PERSISTENCE_FAILED",
                "message": "The confirmation could not be saved. Please retry.",
            },
        ) from exc


@router.post(
    "/scans/{scan_code}/print",
    response_model=PrintScanResponse,
    response_model_by_alias=True,
)
def record_print(scan_code: str) -> PrintScanResponse:
    """Record that a label was printed, and return the QR payload.

    Called by the app *after* the printer reports success, not before. Marking
    a scan PRINTED and then failing to print would leave the audit trail
    claiming something that never happened.

    A print failure never rolls back the confirmed scan — the data is already
    committed and printing is a separate, retryable step (CLAUDE.md §18).
    """
    try:
        stored = repositories.get_scan(scan_code)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "status": "ERROR",
                    "code": "SCAN_NOT_FOUND",
                    "message": f"No scan found with id {scan_code}.",
                },
            )

        repositories.mark_printed(scan_code)
    except PersistenceError as exc:
        logger.exception("Recording print failed", extra={"scanId": scan_code})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "ERROR",
                "code": "PERSISTENCE_FAILED",
                "message": "The print could not be recorded.",
            },
        ) from exc

    # Once a scan is confirmed the operator's values are authoritative,
    # including the ones they deliberately cleared. Falling back to the OCR
    # value for a blank field would resurrect data the operator removed and
    # print it onto the label — the operator is the final authority (§4).
    #
    # The fallback applies only to scans that were never confirmed, where a
    # normalized value is the best available answer.
    if stored.confirmed_at is not None:
        confirmed = {
            name: field.get("confirmedValue") for name, field in stored.fields.items()
        }
    else:
        confirmed = {
            name: field.get("normalizedValue") for name, field in stored.fields.items()
        }

    logger.info("Print recorded", extra={"scanId": scan_code})

    return PrintScanResponse(
        scanId=scan_code,
        status=ProcessingStatus.PRINTED,
        qrPayload=scan_service.build_qr_payload(scan_code, confirmed),
    )


@router.get(
    "/scans/{scan_code}",
    response_model=StoredScanResponse,
    response_model_by_alias=True,
)
def get_scan(scan_code: str) -> StoredScanResponse:
    """Read a stored scan back, including the operator's edits."""
    try:
        stored = repositories.get_scan(scan_code)
    except PersistenceError as exc:
        logger.exception("Scan lookup failed", extra={"scanId": scan_code})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "ERROR",
                "code": "PERSISTENCE_FAILED",
                "message": "The scan could not be read. Please retry.",
            },
        ) from exc

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "status": "ERROR",
                "code": "SCAN_NOT_FOUND",
                "message": f"No scan found with id {scan_code}.",
            },
        )

    return StoredScanResponse(
        scanId=stored.scan_code,
        status=stored.status,
        overallConfidence=stored.overall_confidence,
        imagePath=stored.image_path,
        variantUsed=stored.variant_used,
        deviceId=stored.device_id,
        deviceModel=stored.device_model,
        processingMs=stored.processing_ms,
        failureReason=stored.failure_reason,
        createdAt=stored.created_at,
        confirmedAt=stored.confirmed_at,
        printedAt=stored.printed_at,
        fields=stored.fields,
    )
