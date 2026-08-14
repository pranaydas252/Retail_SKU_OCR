"""SQL Server persistence (CLAUDE.md sections 15 and 16).

The audit trail is the point. For every scan we keep the image path, the raw
OCR tokens with their bounding boxes, the extracted and normalized values, the
confidence figures, and whatever the operator finally confirmed. Field-level
accuracy is the project's main concern, and a failed scan cannot be diagnosed
from a status code alone.

Image binaries never come here — only the filesystem path (section 20).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import pyodbc

from app.db.connection import get_connection
from app.models.response_models import ExtractedField, ProcessingStatus
from app.services.ocr_service import OcrToken

logger = logging.getLogger(__name__)


class PersistenceError(RuntimeError):
    """Database work failed. Carries no driver internals to the client."""


@dataclass
class StoredScan:
    """A scan as read back from the database."""

    scan_id: str
    scan_code: str
    status: str
    overall_confidence: float | None
    image_path: str | None
    variant_used: str | None
    device_id: str | None
    device_model: str | None
    processing_ms: int | None
    failure_reason: str | None
    created_at: datetime
    confirmed_at: datetime | None
    printed_at: datetime | None
    fields: dict[str, dict]


def new_scan_id() -> str:
    """Generate the scan's primary key.

    Generated here rather than by NEWSEQUENTIALID so the identifier is known
    before the insert, which saves a round trip and keeps the OCR pipeline
    usable even when the write later fails.
    """
    return str(uuid.uuid4())


def save_scan(
    scan_id: str,
    scan_code: str,
    status: ProcessingStatus,
    fields: dict[str, ExtractedField],
    tokens: list[OcrToken],
    image_path: str | None = None,
    overall_confidence: float | None = None,
    variant_used: str | None = None,
    device_id: str | None = None,
    device_model: str | None = None,
    processing_ms: int | None = None,
    failure_reason: str | None = None,
) -> None:
    """Persist a scan with its fields and raw OCR tokens.

    Written as a single transaction: a scan row without its tokens would be
    an audit trail that cannot answer the question it exists for.
    """
    try:
        with get_connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO dbo.SkuScan
                        (Id, ScanCode, DeviceId, DeviceModel, ImagePath,
                         ProcessingStatus, OverallConfidence, OcrVariantUsed,
                         FailureReason, ProcessingMs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    scan_id, scan_code, device_id, device_model, image_path,
                    status.value, overall_confidence, variant_used,
                    failure_reason, processing_ms,
                )

                for name, field in fields.items():
                    cursor.execute(
                        """
                        INSERT INTO dbo.SkuScanField
                            (ScanId, FieldName, RawValue, NormalizedValue,
                             Confidence, Source)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        scan_id, name, field.raw_value, field.value,
                        field.confidence, field.source,
                    )

                for index, token in enumerate(tokens):
                    cursor.execute(
                        """
                        INSERT INTO dbo.SkuScanOcr
                            (ScanId, VariantName, TokenIndex, Text,
                             X, Y, Width, Height, Confidence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        scan_id, token.variant, index, token.text,
                        token.x, token.y, token.width, token.height,
                        token.confidence,
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except pyodbc.Error as exc:
        raise PersistenceError(str(exc)) from exc


def confirm_scan(
    scan_code: str, confirmed: dict[str, str | None], validation_notes: dict[str, str]
) -> str:
    """Store the operator's confirmed values and mark the scan CONFIRMED.

    ``WasEdited`` is derived by comparing the confirmed value against what the
    server normalized. That comparison is what makes the manual-correction
    rate of section 24 measurable, so it must not be collapsed into a single
    value column.

    Returns the scan's id. Raises PersistenceError if the scan is unknown or
    the write fails.
    """
    try:
        with get_connection() as connection:
            cursor = connection.cursor()
            try:
                row = cursor.execute(
                    "SELECT Id FROM dbo.SkuScan WHERE ScanCode = ?;", scan_code
                ).fetchone()
                if row is None:
                    raise PersistenceError(f"Unknown scan {scan_code}")

                scan_id = str(row[0])

                for name, value in confirmed.items():
                    note = validation_notes.get(name)
                    updated = cursor.execute(
                        """
                        UPDATE dbo.SkuScanField
                           SET ConfirmedValue = ?,
                               ValidationNote = ?,
                               WasEdited = CASE
                                   WHEN ISNULL(NormalizedValue, '') = ISNULL(?, '')
                                   THEN 0 ELSE 1 END
                         WHERE ScanId = ? AND FieldName = ?;
                        """,
                        value, note, value, scan_id, name,
                    ).rowcount

                    if not updated:
                        # A field the operator supplied that OCR never found.
                        # Recording it as an insert keeps the row complete
                        # rather than silently dropping the operator's work.
                        cursor.execute(
                            """
                            INSERT INTO dbo.SkuScanField
                                (ScanId, FieldName, RawValue, NormalizedValue,
                                 ConfirmedValue, Confidence, Source, WasEdited,
                                 ValidationNote)
                            VALUES (?, ?, NULL, NULL, ?, 0, 'OPERATOR', 1, ?);
                            """,
                            scan_id, name, value, note,
                        )

                cursor.execute(
                    """
                    UPDATE dbo.SkuScan
                       SET ProcessingStatus = ?, ConfirmedAt = SYSUTCDATETIME()
                     WHERE Id = ?;
                    """,
                    ProcessingStatus.CONFIRMED.value, scan_id,
                )

                connection.commit()
                return scan_id
            except Exception:
                connection.rollback()
                raise
    except pyodbc.Error as exc:
        raise PersistenceError(str(exc)) from exc


def mark_printed(scan_code: str) -> None:
    """Record that a label was printed for this scan."""
    try:
        with get_connection() as connection:
            cursor = connection.cursor()
            updated = cursor.execute(
                """
                UPDATE dbo.SkuScan
                   SET ProcessingStatus = ?, PrintedAt = SYSUTCDATETIME()
                 WHERE ScanCode = ?;
                """,
                ProcessingStatus.PRINTED.value, scan_code,
            ).rowcount
            if not updated:
                raise PersistenceError(f"Unknown scan {scan_code}")
            connection.commit()
    except pyodbc.Error as exc:
        raise PersistenceError(str(exc)) from exc


def get_scan(scan_code: str) -> StoredScan | None:
    """Read a scan back with its fields. Returns None if unknown."""
    try:
        with get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(
                """
                SELECT Id, ScanCode, ProcessingStatus, OverallConfidence,
                       ImagePath, OcrVariantUsed, DeviceId, DeviceModel,
                       ProcessingMs, FailureReason, CreatedAt, ConfirmedAt,
                       PrintedAt
                  FROM dbo.SkuScan
                 WHERE ScanCode = ?;
                """,
                scan_code,
            ).fetchone()

            if row is None:
                return None

            scan_id = str(row[0])
            field_rows = cursor.execute(
                """
                SELECT FieldName, RawValue, NormalizedValue, ConfirmedValue,
                       Confidence, Source, WasEdited, ValidationNote
                  FROM dbo.SkuScanField
                 WHERE ScanId = ?
                 ORDER BY FieldName;
                """,
                scan_id,
            ).fetchall()

            fields = {
                field_row[0]: {
                    "rawValue": field_row[1],
                    "normalizedValue": field_row[2],
                    "confirmedValue": field_row[3],
                    "confidence": float(field_row[4]) if field_row[4] is not None else None,
                    "source": field_row[5],
                    "wasEdited": bool(field_row[6]),
                    "validationNote": field_row[7],
                }
                for field_row in field_rows
            }

            return StoredScan(
                scan_id=scan_id,
                scan_code=row[1],
                status=row[2],
                overall_confidence=float(row[3]) if row[3] is not None else None,
                image_path=row[4],
                variant_used=row[5],
                device_id=row[6],
                device_model=row[7],
                processing_ms=row[8],
                failure_reason=row[9],
                created_at=row[10],
                confirmed_at=row[11],
                printed_at=row[12],
                fields=fields,
            )
    except pyodbc.Error as exc:
        raise PersistenceError(str(exc)) from exc
