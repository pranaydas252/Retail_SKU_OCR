"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import routes_health, routes_scans
from app.config import get_settings
from app.db.connection import check_health
from app.services.ocr_service import get_ocr_service
from app.utils.logging import configure_logging

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info(
        "Starting D-Mart SKU OCR backend",
        extra={
            "detectionModel": settings.ocr_det_model_name,
            "recognitionModel": settings.ocr_rec_model_name,
            "variants": settings.variant_list,
        },
    )

    settings.image_root.mkdir(parents=True, exist_ok=True)

    if check_health():
        logger.info("Database reachable")
    else:
        logger.warning(
            "Database unreachable; scan codes will use the local fallback"
        )

    if settings.ocr_warmup_on_startup:
        # Blocking on purpose. On first run this downloads models, and a
        # server that accepts requests mid-download would hang the operator's
        # first scan instead of simply starting a little later.
        try:
            get_ocr_service().warmup()
        except Exception:
            logger.exception("OCR warmup failed; models will load on first scan")

    yield

    logger.info("Shutting down")


app = FastAPI(
    title="D-Mart SKU Label OCR",
    version="0.1.0",
    description="Server-backed OCR for D-Mart SKU labels. R&D proof of concept.",
    lifespan=lifespan,
)

app.include_router(routes_health.router, prefix=API_PREFIX)
app.include_router(routes_scans.router, prefix=API_PREFIX)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """Last resort. Log the detail, return a safe body (section 22)."""
    logger.exception("Unhandled error", extra={"path": str(request.url.path)})
    return JSONResponse(
        status_code=500,
        content={
            "status": "ERROR",
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred.",
        },
    )
