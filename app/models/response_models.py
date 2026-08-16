"""API response models.

The field-level shape mirrors CLAUDE.md section 4 so the Kotlin data classes
map one-to-one.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ProcessingStatus(str, Enum):
    """Scan outcomes.

    NO_TEXT_DETECTED is a successful response, not an error — the operator is
    asked to recapture (CLAUDE.md section 22).
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    NO_TEXT_DETECTED = "NO_TEXT_DETECTED"
    FAILED = "FAILED"
    CONFIRMED = "CONFIRMED"
    PRINTED = "PRINTED"


class ConfidenceBand(str, Enum):
    """UI treatment bands from CLAUDE.md section 12.

    Engineering defaults, not calibrated probabilities. Tune against real
    label images before trusting them.
    """

    HIGH = "HIGH"
    REVIEW = "REVIEW"
    LOW = "LOW"


class BoundingBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class OcrTokenModel(BaseModel):
    """A single recognized text region.

    Spatial data is preserved deliberately — field extraction associates
    labels with values by position, which is impossible once OCR output is
    flattened to a string (CLAUDE.md section 6).
    """

    text: str
    bbox: BoundingBox
    confidence: float
    variant: str | None = None


class ExtractedField(BaseModel):
    """One extracted label field.

    Field keys are NOT a fixed set. The rule-based extractor emits the five
    known SKU fields, but a document-understanding model can return whatever
    key/value pairs a given pack happens to carry — `Net Contents`, a licence
    number, `Batch No.` under a name nobody predicted.

    `displayName` is what makes that work end to end: the server sends a human
    label alongside every key, so the app can render a field it has never seen
    without shipping a translation for it.
    """

    value: str | None = None
    confidence: float = 0.0
    band: ConfidenceBand = ConfidenceBand.LOW
    source: str = "OCR_RULES"
    raw_value: str | None = Field(default=None, alias="rawValue")
    display_name: str | None = Field(default=None, alias="displayName")
    #: True for the core fields the POC always asks about, so the app can show
    #: them as empty rather than omitting them when a pack lacks one.
    expected: bool = False

    model_config = {"populate_by_name": True}


class ScanResponse(BaseModel):
    scan_id: str = Field(alias="scanId")
    status: ProcessingStatus
    overall_confidence: float | None = Field(default=None, alias="overallConfidence")
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    tokens: list[OcrTokenModel] = Field(default_factory=list)
    timings: dict[str, int] = Field(default_factory=dict)
    variant_used: str | None = Field(default=None, alias="variantUsed")
    # False when OCR succeeded but the database write did not. The results are
    # still valid and displayable; confirming them will fail until the
    # database is back.
    persisted: bool = True
    message: str | None = None

    model_config = {"populate_by_name": True}


class ConfirmScanResponse(BaseModel):
    scan_id: str = Field(alias="scanId")
    status: ProcessingStatus
    persisted: bool = True
    validation_notes: dict[str, list[str]] = Field(
        default_factory=dict, alias="validationNotes"
    )
    qr_payload: str | None = Field(default=None, alias="qrPayload")

    model_config = {"populate_by_name": True}


class PrintScanResponse(BaseModel):
    scan_id: str = Field(alias="scanId")
    status: ProcessingStatus
    qr_payload: str | None = Field(default=None, alias="qrPayload")

    model_config = {"populate_by_name": True}


class StoredScanResponse(BaseModel):
    """A scan read back from the database, including the operator's edits."""

    scan_id: str = Field(alias="scanId")
    status: ProcessingStatus
    overall_confidence: float | None = Field(default=None, alias="overallConfidence")
    image_path: str | None = Field(default=None, alias="imagePath")
    variant_used: str | None = Field(default=None, alias="variantUsed")
    device_id: str | None = Field(default=None, alias="deviceId")
    device_model: str | None = Field(default=None, alias="deviceModel")
    processing_ms: int | None = Field(default=None, alias="processingMs")
    failure_reason: str | None = Field(default=None, alias="failureReason")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    confirmed_at: datetime | None = Field(default=None, alias="confirmedAt")
    printed_at: datetime | None = Field(default=None, alias="printedAt")
    fields: dict[str, dict] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class HealthResponse(BaseModel):
    status: str = "UP"
    ocr_ready: bool = Field(default=False, alias="ocrReady")
    detection_model: str | None = Field(default=None, alias="detectionModel")
    recognition_model: str | None = Field(default=None, alias="recognitionModel")

    model_config = {"populate_by_name": True}
