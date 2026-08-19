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


def _agreement_bonus() -> float:
    return float(load_config().get("engine_agreement_bonus", 0.15))


def _conflict_penalty() -> float:
    return float(load_config().get("engine_conflict_penalty", 0.35))


def score_field(
    candidate: FieldCandidate,
    format_ok: bool,
    consistency_ok: bool,
    agreement: bool | None = None,
) -> float:
    """Confidence for one extracted field, in [0, 1].

    ``agreement`` is the two-engine signal, and is None when only one engine
    ran or only one produced this field:

        True   both engines read the same value
        False  both produced a value and they differ
        None   no second opinion exists

    Section 12 lists cross-variant agreement as a confidence signal, and two
    independent engines are a stronger version of the same idea. It is applied
    as an adjustment rather than a weighted signal because it is frequently
    absent, and a weight would have to be silently redistributed every time —
    which would make two scans with the same evidence score differently for
    reasons nobody could see.
    """
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

    # Two engines that read the same string are hard to fool at once, and two
    # that read different strings have told us one of them is wrong without
    # saying which. The bonus closes part of the remaining gap rather than
    # adding a fixed amount, so corroboration can never carry a field that
    # every other signal rates poorly.
    if agreement is True:
        score += (1.0 - score) * _agreement_bonus()
    elif agreement is False:
        score *= 1.0 - _conflict_penalty()

    return round(max(0.0, min(1.0, score)), 4)


def band(
    score: float,
    uncorroborated: bool = False,
    second_engine_ran: bool = True,
) -> ConfidenceBand:
    """Map a score to its UI treatment band.

    Thresholds are tuned against the gated corpus by
    ``scripts/calibrate_confidence.py``; see app/config.py for what the
    measurement said.

    ``uncorroborated`` marks a value that two engines did not agree on, on a
    scan where both ran. Such a value is capped at REVIEW however well it
    scores, so HIGH means exactly one thing: both engines read this, and read
    it the same.

    That cap is not a hedge, it is the measured failure profile. Agreement is
    by a wide margin the strongest predictor of correctness in this pipeline.
    Measured on the 19-image gated corpus with both engines running:

        both agreed    31 values   31 correct    0 wrong   100%
        contested      10 values    4 correct    6 wrong    40%
        one engine     27 values   16 correct   11 wrong    59%

    Agreement also predicts far better than the score does. The two
    highest-scoring WRONG values in that corpus score 0.869 and 0.783, above
    any threshold worth setting, and both are uncorroborated — so no threshold
    can exclude them and this rule excludes both by construction.

    Contested values are capped too, and that is not obvious: two engines both
    producing a value looks like more evidence than one. It is not. Contested
    measures WORSE than solo, because engines disagree precisely where the
    print is hard to read. The screen already treats those fields as open
    questions — both readings shown, neither preselected — and a HIGH band on
    a field the UI is asking about would contradict it.

    The cap was originally VLM-only, on the argument that an engine which never
    declines cannot signal its own uncertainty. That argument was right and is
    unchanged; it was simply incomplete. PP-OCRv5 alone measures no better, and
    its solo errors are the confident ones — a 0.9974-confidence price read off
    the wrong row is not something recognition confidence can catch.

    Inert when only one engine ran: capping every field on a PP-OCRv5-only scan
    would empty the HIGH band rather than describe it. That path pays for the
    missing rule with a higher threshold instead — see
    confidence_high_threshold_single_engine in app/config.py, and note that
    0.75 is only safe BECAUSE the agreement rule is doing the work.
    """
    settings = get_settings()
    threshold = (
        settings.confidence_high_threshold
        if second_engine_ran
        else settings.confidence_high_threshold_single_engine
    )
    if score >= threshold and not uncorroborated:
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
