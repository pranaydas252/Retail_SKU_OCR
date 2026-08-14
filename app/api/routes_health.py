"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.models.response_models import HealthResponse
from app.services.ocr_service import get_ocr_service

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness plus OCR readiness.

    Unauthenticated by design so IIS and monitoring can probe it. Reports
    whether models are loaded, which distinguishes "starting up" from "wedged"
    during the first-run model download.
    """
    settings = get_settings()
    return HealthResponse(
        status="UP",
        ocrReady=get_ocr_service().is_ready,
        detectionModel=settings.ocr_det_model_name,
        recognitionModel=settings.ocr_rec_model_name,
    )
