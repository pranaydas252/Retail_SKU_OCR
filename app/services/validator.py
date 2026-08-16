"""Deterministic validation after extraction (CLAUDE.md section 11).

Validation never repairs a value and never invents one. It reports what is
wrong so confidence can fall and the operator can correct it.

The governing bias, from section 24: a wrong expiry date is more serious than
a recapture request. Prefer flagging for review over accepting silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date

_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE_CHARSET = re.compile(r"^[A-Z0-9\-/]+$")

# A label printed more than this far in the past or future is more likely an
# OCR misread than a real date.
_MIN_YEAR = 2000
_MAX_YEAR = 2100


@dataclass
class ValidationResult:
    """Per-field and cross-field outcome."""

    field_notes: dict[str, list[str]] = dataclass_field(default_factory=dict)
    consistency_ok: bool = True
    consistency_notes: list[str] = dataclass_field(default_factory=list)

    def note(self, field: str, message: str) -> None:
        self.field_notes.setdefault(field, []).append(message)

    def is_valid(self, field: str) -> bool:
        return not self.field_notes.get(field)


def _parse_iso(value: str) -> tuple[int, int, int | None] | None:
    if _ISO_DAY.match(value):
        year, month, day = value.split("-")
        return int(year), int(month), int(day)
    if _ISO_MONTH.match(value):
        year, month = value.split("-")
        return int(year), int(month), None
    return None


def _as_comparable(value: str) -> date | None:
    """Month-precision dates compare on their first day."""
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    year, month, day = parsed
    try:
        return date(year, month, day or 1)
    except ValueError:
        return None


def validate_date(value: str) -> list[str]:
    parsed = _parse_iso(value)
    if parsed is None:
        return ["date does not match YYYY-MM or YYYY-MM-DD"]

    year, month, _ = parsed
    problems = []
    if not 1 <= month <= 12:
        problems.append("month out of range")
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        problems.append(f"year {year} outside plausible range")
    return problems


def validate_currency(value: str) -> list[str]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ["MRP is not numeric"]

    if amount <= 0:
        return ["MRP must be positive"]
    if amount > 1_000_000:
        return ["MRP implausibly large"]
    return []


def validate_code(value: str) -> list[str]:
    if not _CODE_CHARSET.match(value):
        return ["code contains disallowed characters"]
    if len(value) < 2:
        return ["code too short to be meaningful"]
    if len(value) > 32:
        return ["code implausibly long"]
    return []


_VALIDATORS = {
    "date": validate_date,
    "currency": validate_currency,
    "code": validate_code,
}


def validate(values: dict[str, str | None], types: dict[str, str]) -> ValidationResult:
    """Validate normalized values and their cross-field relationships.

    ``values`` maps field name to normalized value, with None for fields that
    could not be extracted. Missing fields are recorded explicitly rather than
    quietly omitted (section 11).
    """
    result = ValidationResult()

    for field, value in values.items():
        if value is None:
            result.note(field, "not found")
            continue

        validator = _VALIDATORS.get(types.get(field, "code"))
        for problem in validator(value) if validator else []:
            result.note(field, problem)

    _check_date_order(values, result)
    return result


def _check_date_order(values: dict[str, str | None], result: ValidationResult) -> None:
    """manufacturingDate must precede expiryDate (section 11).

    A failure here marks the result for review. It is never silently accepted,
    and neither date is "corrected" to satisfy the rule — the extraction is
    what is suspect, not the calendar.
    """
    mfg_raw = values.get("manufacturingDate")
    exp_raw = values.get("expiryDate")
    if not mfg_raw or not exp_raw:
        return

    mfg = _as_comparable(mfg_raw)
    exp = _as_comparable(exp_raw)
    if mfg is None or exp is None:
        return

    if mfg >= exp:
        result.consistency_ok = False
        message = (
            f"manufacturing date {mfg_raw} is not before expiry date {exp_raw}"
        )
        result.consistency_notes.append(message)
        result.note("manufacturingDate", message)
        result.note("expiryDate", message)
