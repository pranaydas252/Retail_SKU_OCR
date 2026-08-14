"""API authentication.

Kept isolated from business logic (CLAUDE.md section 19) so the POC's static
key can be swapped for a real scheme without touching the pipeline.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject requests without a valid API key.

    When no key is configured the check is skipped and a warning is logged.
    That keeps local development frictionless while making an unprotected
    deployment loud rather than silent.
    """
    settings = get_settings()

    if not settings.api_key:
        logger.warning("API_KEY is not configured; endpoint is unauthenticated")
        return

    # Constant-time comparison: a plain == leaks key content through timing.
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "status": "ERROR",
                "code": "UNAUTHORIZED",
                "message": "A valid API key is required.",
            },
        )
