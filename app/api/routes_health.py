"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.models.response_models import HealthResponse
from app.services.ocr_service import get_ocr_service
from app.services.vlm_service import get_vlm_service

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness plus OCR readiness.

    Unauthenticated by design so IIS and monitoring can probe it. Reports
    whether models are loaded, which distinguishes "starting up" from "wedged"
    during the first-run model download.
    """
    settings = get_settings()

    # Reachability is probed rather than assumed. The scan path deliberately
    # swallows a VLM failure so one bad call cannot cost the operator a scan,
    # which means a stopped Ollama is invisible from the results alone - every
    # scan just quietly becomes single-engine. This endpoint is where that
    # becomes visible.
    vlm_ready = settings.vlm_enabled and get_vlm_service().is_available()

    return HealthResponse(
        status="UP",
        ocrReady=get_ocr_service().is_ready,
        detectionModel=settings.ocr_det_model_name,
        recognitionModel=settings.ocr_rec_model_name,
        vlmEnabled=settings.vlm_enabled,
        vlmReady=vlm_ready,
        vlmModel=settings.vlm_model if settings.vlm_enabled else None,
    )
