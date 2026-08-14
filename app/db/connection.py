"""SQL Server connectivity.

Phase 1 uses this only to allocate scan codes from the ``SeqScanCode``
sequence. Scan, field, and OCR-token persistence lands in Phase 3 via
``repositories.py``.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import pyodbc

from app.config import get_settings

logger = logging.getLogger(__name__)

_fallback_lock = threading.Lock()
_fallback_counter = 0


@contextmanager
def get_connection() -> Iterator[pyodbc.Connection]:
    """Open a connection from the configured connection string."""
    settings = get_settings()
    if not settings.sql_connection_string:
        raise RuntimeError("SQL_CONNECTION_STRING is not configured.")

    connection = pyodbc.connect(settings.sql_connection_string, timeout=5)
    try:
        yield connection
    finally:
        connection.close()


def check_health() -> bool:
    """True if the database answers. Never raises."""
    try:
        with get_connection() as connection:
            connection.cursor().execute("SELECT 1").fetchone()
        return True
    except Exception as exc:
        logger.warning("Database health check failed", extra={"error": str(exc)})
        return False


def next_scan_code() -> str:
    """Allocate the next SCAN-nnnnnn identifier.

    Falls back to a process-local counter when the database is unreachable so
    the OCR pipeline stays testable without SQL Server. Fallback codes carry a
    ``-LOCAL`` suffix: they are deliberately distinguishable, because they are
    not unique across restarts and must never reach persisted data.
    """
    try:
        with get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT NEXT VALUE FOR dbo.SeqScanCode;")
            value = int(cursor.fetchone()[0])
            return f"SCAN-{value:06d}"
    except Exception as exc:
        global _fallback_counter
        with _fallback_lock:
            _fallback_counter += 1
            value = _fallback_counter
        logger.warning(
            "Scan code sequence unavailable, using local fallback",
            extra={"error": str(exc), "fallbackValue": value},
        )
        return f"SCAN-{value:06d}-LOCAL"
