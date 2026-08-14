"""Application-level confidence scoring (CLAUDE.md section 12).

Raw OCR confidence is not exposed as the confidence. A recognizer can be
completely certain about text it read perfectly and that the extractor then
attached to the wrong field — OCR confidence says nothing about association
quality, format validity, or cross-field agreement.

The score combines five signals:

    ocr          recognition confidence of the value (and its label)
    label_match  exact alias hit vs. a looser prefix match
    spatial      same-line beats below; near beats far
    format       the value normalized cleanly and passed validation
    consistency  cross-field agreement, e.g. mfg < exp

Weights live in config/field_aliases.yaml and are engineering defaults. They
are NOT calibrated probabilities, and section 12 forbids presenting them as
such. Tune against real label images before trusting the number.
"""

from __future__ import annotations

from app.config import get_settings
from app.models.response_models import ConfidenceBand
from app.services.field_extractor import FieldCandidate, load_config

_DEFAULT_WEIGHTS = {
    "ocr": 0.40,
    "label_match": 0.20,
    "spatial": 0.20,
    "format": 0.15,
    "consistency": 0.05,
}


def _weights() -> dict[str, float]:
    """Normalized weights, so the config need not sum to 1."""
    configured = {**_DEFAULT_WEIGHTS, **(load_config().get("confidence_weights") or {})}
    total = sum(configured.values()) or 1.0
    return {key: value / total for key, value in configured.items()}


def _ambiguity_penalty() -> float:
    return float(load_config().get("ambiguous_date_penalty", 0.25))


def score_field(
    candidate: FieldCandidate,
    format_ok: bool,
    consistency_ok: bool,
) -> float:
    """Confidence for one extracted field, in [0, 1]."""
    weights = _weights()

    signals = {
        "ocr": max(0.0, min(1.0, candidate.ocr_confidence)),
        "label_match": max(0.0, min(1.0, candidate.label_match_quality)),
        "spatial": max(0.0, min(1.0, candidate.spatial_quality)),
        "format": 1.0 if format_ok else 0.0,
        "consistency": 1.0 if consistency_ok else 0.0,
    }

    score = sum(weights[key] * value for key, value in signals.items())

    # A date resolved under an assumed convention is genuinely less certain,
    # even when every other signal is perfect. Costing it confidence is what
    # pushes it into the operator's review band instead of silent acceptance.
    if candidate.normalized.is_ambiguous:
        score *= 1.0 - _ambiguity_penalty()

    return round(max(0.0, min(1.0, score)), 4)


def band(score: float) -> ConfidenceBand:
    """Map a score to its UI treatment band.

    Thresholds are initial engineering defaults from section 12 and must be
    tuned against real label images.
    """
    settings = get_settings()
    if score >= settings.confidence_high_threshold:
        return ConfidenceBand.HIGH
    if score >= settings.confidence_review_threshold:
        return ConfidenceBand.REVIEW
    return ConfidenceBand.LOW


def overall(scores: dict[str, float], found: set[str] | None = None) -> float | None:
    """Overall confidence in the fields that were actually extracted.

    Deliberately the MINIMUM, not the mean. A scan whose expiry date is a guess
    is not rescued by four other fields being perfect, and averaging would hide
    exactly the failure that matters most.

    Scored over FOUND fields only, not over every required field. Most real
    packs do not carry all five — a snack pouch typically prints a batch code
    and no separate lot code — so including missing fields would drive overall
    confidence to zero on almost every genuine label and make the number
    meaningless. Completeness is reported separately: every required field
    appears in the response, with a null value when it was not found.
    """
    if not scores:
        return None

    considered = (
        [scores[f] for f in found if f in scores] if found is not None
        else list(scores.values())
    )
    if not considered:
        return None

    return round(min(considered), 4)
