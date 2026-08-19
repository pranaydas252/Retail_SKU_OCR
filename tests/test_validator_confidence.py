"""Validation and confidence scoring tests (sections 11 and 12)."""

from __future__ import annotations

import pytest

from app.models.response_models import ConfidenceBand
from app.services import confidence, validator
from app.services.field_extractor import FieldCandidate
from app.services.normalizer import NormalizedValue
from app.services.ocr_service import OcrToken

TYPES = {
    "batchNumber": "code",
    "manufacturingDate": "date",
    "expiryDate": "date",
    "lotCode": "code",
    "mrp": "currency",
}


class TestFieldValidation:
    @pytest.mark.parametrize("value", ["2026-07", "2026-07-25"])
    def test_accepts_iso_dates(self, value):
        assert validator.validate_date(value) == []

    @pytest.mark.parametrize("value", ["07/2026", "2026", "26-07", "not-a-date"])
    def test_rejects_non_iso_dates(self, value):
        assert validator.validate_date(value)

    def test_rejects_implausible_year(self):
        assert validator.validate_date("1850-07")

    @pytest.mark.parametrize("value", ["245.00", "1.50", "99999.99"])
    def test_accepts_valid_prices(self, value):
        assert validator.validate_currency(value) == []

    @pytest.mark.parametrize("value", ["0.00", "-5.00", "abc", "9999999.00"])
    def test_rejects_invalid_prices(self, value):
        assert validator.validate_currency(value)

    @pytest.mark.parametrize("value", ["A23C91", "K18P2", "AB-123/C"])
    def test_accepts_valid_codes(self, value):
        assert validator.validate_code(value) == []

    @pytest.mark.parametrize("value", ["A", "code with spaces", "A" * 40])
    def test_rejects_invalid_codes(self, value):
        assert validator.validate_code(value)


class TestMissingFields:
    def test_missing_field_is_recorded_explicitly(self):
        result = validator.validate({"batchNumber": None}, TYPES)

        assert not result.is_valid("batchNumber")
        assert "not found" in result.field_notes["batchNumber"]


class TestDateConsistency:
    def test_manufacturing_before_expiry_passes(self):
        result = validator.validate(
            {"manufacturingDate": "2026-07", "expiryDate": "2028-06"}, TYPES
        )
        assert result.consistency_ok

    def test_manufacturing_after_expiry_fails(self):
        result = validator.validate(
            {"manufacturingDate": "2028-06", "expiryDate": "2026-07"}, TYPES
        )

        assert not result.consistency_ok
        assert result.consistency_notes
        # Both dates are implicated; the extraction is what is suspect.
        assert not result.is_valid("manufacturingDate")
        assert not result.is_valid("expiryDate")

    def test_equal_dates_fail(self):
        result = validator.validate(
            {"manufacturingDate": "2026-07", "expiryDate": "2026-07"}, TYPES
        )
        assert not result.consistency_ok

    def test_one_date_missing_skips_the_check(self):
        result = validator.validate(
            {"manufacturingDate": "2026-07", "expiryDate": None}, TYPES
        )
        assert result.consistency_ok

    def test_mixed_precision_dates_compare(self):
        result = validator.validate(
            {"manufacturingDate": "2026-07-15", "expiryDate": "2028-06"}, TYPES
        )
        assert result.consistency_ok


def candidate(
    ocr: float = 0.99,
    label_quality: float = 1.0,
    spatial: float = 1.0,
    ambiguous: bool = False,
) -> FieldCandidate:
    token = OcrToken("X", 0, 0, 10, 10, ocr)
    return FieldCandidate(
        field="batchNumber",
        raw_value="X",
        normalized=NormalizedValue("X", is_ambiguous=ambiguous),
        label_token=token,
        value_token=token,
        strategy="same_line",
        label_match_quality=label_quality,
        spatial_quality=spatial,
    )


class TestConfidenceScoring:
    def test_perfect_signals_score_near_one(self):
        assert confidence.score_field(candidate(), True, True) > 0.98

    def test_format_failure_lowers_score(self):
        good = confidence.score_field(candidate(), True, True)
        bad = confidence.score_field(candidate(), False, True)
        assert bad < good

    def test_consistency_failure_lowers_score(self):
        good = confidence.score_field(candidate(), True, True)
        bad = confidence.score_field(candidate(), True, False)
        assert bad < good

    def test_weak_spatial_association_lowers_score(self):
        near = confidence.score_field(candidate(spatial=1.0), True, True)
        far = confidence.score_field(candidate(spatial=0.3), True, True)
        assert far < near

    def test_low_ocr_confidence_lowers_score(self):
        high = confidence.score_field(candidate(ocr=0.99), True, True)
        low = confidence.score_field(candidate(ocr=0.5), True, True)
        assert low < high

    def test_ambiguity_penalty_applies(self):
        certain = confidence.score_field(candidate(), True, True)
        ambiguous = confidence.score_field(candidate(ambiguous=True), True, True)
        assert ambiguous < certain

    def test_ocr_confidence_alone_does_not_carry_the_score(self):
        # Section 12: perfect recognition of a badly-associated value must not
        # produce high confidence.
        result = confidence.score_field(
            candidate(ocr=1.0, label_quality=0.0, spatial=0.0), False, False
        )
        assert result < 0.5

    def test_score_stays_in_range(self):
        for ocr in (0.0, 0.5, 1.0):
            score = confidence.score_field(candidate(ocr=ocr), True, True)
            assert 0.0 <= score <= 1.0


class TestBands:
    def test_high_band(self):
        assert confidence.band(0.97) == ConfidenceBand.HIGH

    def test_review_band(self):
        assert confidence.band(0.60) == ConfidenceBand.REVIEW

    def test_low_band(self):
        assert confidence.band(0.40) == ConfidenceBand.LOW

    def test_boundaries_are_inclusive_at_the_bottom(self):
        assert confidence.band(0.75) == ConfidenceBand.HIGH
        assert confidence.band(0.50) == ConfidenceBand.REVIEW


class TestOverallConfidence:
    def test_uses_minimum_not_mean(self):
        # A guessed expiry date must not be averaged away by good fields.
        scores = {"a": 0.99, "b": 0.99, "c": 0.60}
        assert confidence.overall(scores, {"a", "b", "c"}) == 0.60

    def test_scores_only_found_fields(self):
        # Most real packs lack at least one of the five fields; counting the
        # missing ones would zero out almost every genuine label.
        scores = {"a": 0.95, "b": 0.0}
        assert confidence.overall(scores, {"a"}) == 0.95

    def test_returns_none_when_nothing_found(self):
        assert confidence.overall({"a": 0.0}, set()) is None
        assert confidence.overall({}) is None


class TestUncorroboratedIsCapped:
    """HIGH means two engines read the value and read it the same.

    Measured on the 19-image gated corpus with both engines running:

        both agreed    31 values   31 correct    0 wrong   100%
        contested      10 values    4 correct    6 wrong    40%
        one engine     27 values   16 correct   11 wrong    59%

    Agreement predicts correctness far better than the score does — the two
    highest-scoring wrong values in that corpus, 0.869 and 0.783, are both
    uncorroborated, so no threshold can exclude them and this rule excludes
    both outright.
    """

    def test_an_uncorroborated_value_is_capped_at_review(self):
        assert confidence.band(0.99, uncorroborated=True) == (
            ConfidenceBand.REVIEW
        )

    def test_the_same_score_reaches_high_when_corroborated(self):
        assert confidence.band(0.99) == ConfidenceBand.HIGH

    def test_the_cap_does_not_rescue_a_poor_score(self):
        # It caps the top of the range, it does not lift the bottom: a badly
        # scoring VLM-only value stays LOW rather than being promoted.
        assert confidence.band(0.10, uncorroborated=True) == (
            ConfidenceBand.LOW
        )
