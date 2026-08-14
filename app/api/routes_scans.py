"""Scan endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.security import require_api_key
from app.models.response_models import ScanResponse
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
            data, device_id=deviceId, device_model=deviceModel
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
