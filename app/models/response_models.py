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


class FieldSource(str, Enum):
    """Where a stored field value came from (CLAUDE.md section 16).

    Provenance, not decoration. The audit trail exists so a wrong field can be
    diagnosed after the fact, and these differ enormously in how much they can
    be trusted: BARCODE either decoded or it did not, OCR_RULES is a reading
    that may be wrong, DERIVED_RULE was computed rather than read at all.

    Kept in step with the constants in the Android ApiModels.kt, which compares
    them as literals — see tests/test_wire_contract.py.
    """

    #: Read by the rule extractor from recognised text.
    OCR_RULES = "OCR_RULES"
    #: Computed from another field and a rule printed on the pack, such as a
    #: "best before N months" shelf life. Never read directly.
    DERIVED_RULE = "DERIVED_RULE"
    #: Decoded from the pack's barcode. The only source here that is not a
    #: reading, and so the only one with nothing for an operator to check.
    BARCODE = "BARCODE"
    #: Typed or corrected by the operator at confirmation.
    OPERATOR = "OPERATOR"
    #: Extraction ran and found nothing.
    NOT_FOUND = "NOT_FOUND"


#: The one field that is decoded rather than recognised. Defined here so the
#: persistence layer and the extractor agree on the name without importing each
#: other; it must match the key in config/field_aliases.yaml.
BARCODE_FIELD = "skuCode"


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

    #: Which recognition engines produced this value: "OCR", "VLM", or both.
    #:
    #: Diagnostic, and additive on the wire — the app ignores unknown keys, so
    #: this reaches the debugging workflow without an app release. Two engines
    #: agreeing is the strongest signal available for a field (section 12), and
    #: an engineer looking at a bad scan needs to know which one answered.
    engines: list[str] = Field(default_factory=list)

    #: What the other engine read, when the two disagreed. None otherwise.
    conflict_value: str | None = Field(default=None, alias="conflictValue")
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
