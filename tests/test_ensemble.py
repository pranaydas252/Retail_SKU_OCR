"""Two-engine merging and the agreement signal (CLAUDE.md section 12)."""

from __future__ import annotations

from app.services import confidence, ensemble
from app.services.field_extractor import FieldCandidate
from app.services.normalizer import NormalizedValue


def candidate(value: str, *, ocr: float = 0.9, ambiguous: bool = False) -> FieldCandidate:
    from app.services.ocr_service import OcrToken

    token = OcrToken(value, 0, 0, 100, 30, ocr)
    return FieldCandidate(
        field="expiryDate",
        raw_value=value,
        normalized=NormalizedValue(value, is_ambiguous=ambiguous),
        label_token=None,
        value_token=token,
        strategy="same_line",
        label_match_quality=1.0,
        spatial_quality=1.0,
    )


class TestMerge:
    def test_both_engines_agreeing_is_recorded(self):
        merged = ensemble.merge(
            {"expiryDate": candidate("2026-10")},
            {"expiryDate": candidate("2026-10")},
        )
        field = merged["expiryDate"]

        assert field.corroborated
        assert field.engines == (ensemble.PRIMARY, ensemble.SECONDARY)
        assert field.conflict is None

    def test_separator_differences_are_not_a_disagreement(self):
        # Two engines must not be recorded as disagreeing because one wrote a
        # slash. This is the same comparison the accuracy harness uses, so
        # "agreed" here means what "correct" means there.
        merged = ensemble.merge(
            {"expiryDate": candidate("2026-10")},
            {"expiryDate": candidate("2026/10")},
        )
        assert merged["expiryDate"].corroborated

    def test_a_disagreement_keeps_the_primary_and_records_the_other(self):
        merged = ensemble.merge(
            {"expiryDate": candidate("2026-10")},
            {"expiryDate": candidate("2027-01")},
        )
        field = merged["expiryDate"]

        assert field.candidate.normalized.value == "2026-10"
        assert field.contested
        assert field.conflict == "2027-01"
        assert not field.corroborated

    def test_a_field_only_the_vlm_found_is_taken(self):
        # The entire reason the second engine runs: on packs PP-OCRv5 cannot
        # read, it is the only engine with an answer.
        merged = ensemble.merge({}, {"expiryDate": candidate("2026-10")})
        field = merged["expiryDate"]

        assert field.candidate.normalized.value == "2026-10"
        assert field.engines == (ensemble.SECONDARY,)
        assert not field.corroborated

    def test_a_field_only_paddle_found_is_taken(self):
        merged = ensemble.merge({"expiryDate": candidate("2026-10")}, {})
        assert merged["expiryDate"].engines == (ensemble.PRIMARY,)

    def test_no_engine_means_no_field(self):
        assert ensemble.merge({}, {}) == {}


class TestAgreementChangesConfidence:
    """The signal has to move the number, or merging is bookkeeping."""

    def _score(self, agreement):
        return confidence.score_field(
            candidate("2026-10"), format_ok=True, consistency_ok=True,
            agreement=agreement,
        )

    def test_agreement_raises_confidence(self):
        assert self._score(True) > self._score(None)

    def test_disagreement_lowers_confidence(self):
        assert self._score(False) < self._score(None)

    def test_a_contested_field_lands_in_review_or_below(self):
        # The point of the penalty. A value the two engines contradict each
        # other on must not reach the operator looking settled.
        assert confidence.band(self._score(False)) != confidence.band(1.0)

    def test_agreement_cannot_rescue_a_bad_field(self):
        # The bonus closes a fraction of the remaining gap rather than adding a
        # fixed amount, so corroboration never carries a field every other
        # signal rates poorly.
        weak = candidate("2026-10", ocr=0.05, ambiguous=True)
        agreed = confidence.score_field(
            weak, format_ok=False, consistency_ok=False, agreement=True
        )
        assert agreed < get_review_threshold()

    def test_no_second_opinion_leaves_the_score_untouched(self):
        plain = confidence.score_field(
            candidate("2026-10"), format_ok=True, consistency_ok=True
        )
        assert plain == self._score(None)


def get_review_threshold() -> float:
    from app.config import get_settings

    return get_settings().confidence_review_threshold


class TestPipelineWiring:
    """The ensemble reaching the API response, not just the merge module."""

    def test_a_single_engine_scan_is_unchanged(self):
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        tokens = [
            OcrToken("BATCH NO", 20, 100, 180, 30, 0.97),
            OcrToken("A23C91", 300, 100, 120, 30, 0.97),
        ]
        fields, _ = extract_and_score(tokens)

        assert fields["batchNumber"].value == "A23C91"
        assert fields["batchNumber"].engines == ["OCR"]
        assert fields["batchNumber"].conflict_value is None

    def test_a_field_only_the_second_engine_read_still_reaches_the_operator(self):
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        # PP-OCRv5 recognised the label but nothing usable beside it, which is
        # the measured Eno case. The VLM read the value.
        primary = [OcrToken("Batch No.:", 20, 100, 180, 30, 0.97)]
        secondary = [OcrToken("BATCH NO 685L289A", 0, 0, 200, 30, 0.0)]

        fields, _ = extract_and_score(primary, secondary)

        assert fields["batchNumber"].value == "685L289A"
        assert fields["batchNumber"].engines == ["VLM"]

    def test_agreement_is_reported_on_the_wire(self):
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        primary = [OcrToken("MFG 07/2026", 20, 100, 200, 30, 0.97)]
        secondary = [OcrToken("MFG 07/2026", 0, 0, 200, 30, 0.0)]

        fields, _ = extract_and_score(primary, secondary)
        assert fields["manufacturingDate"].engines == ["OCR", "VLM"]

    def test_a_contested_field_carries_the_other_reading(self):
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        primary = [OcrToken("MFG 07/2026", 20, 100, 200, 30, 0.97)]
        secondary = [OcrToken("MFG 01/2027", 0, 0, 200, 30, 0.0)]

        fields, _ = extract_and_score(primary, secondary)
        field = fields["manufacturingDate"]

        assert field.value == "2026-07"          # primary wins the field
        assert field.conflict_value == "2027-01"  # the disagreement is recorded

    def test_only_an_agreed_value_can_reach_high(self):
        """HIGH means both engines read it and read it the same.

        Measured on the 19-image gated corpus: agreed values were 31 of 31
        correct, contested 4 of 10, and single-engine 16 of 27. Contested
        measures WORSE than solo -- engines disagree exactly where the print is
        hard -- so "both engines produced something" is not the rule; "both
        produced the same thing" is.
        """
        from app.models.response_models import ConfidenceBand
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        label = OcrToken("MFG", 20, 100, 60, 30, 0.99)
        value = OcrToken("07/2026", 90, 100, 140, 30, 0.99)

        agreed, _ = extract_and_score(
            [label, value], [OcrToken("MFG 07/2026", 0, 0, 200, 30, 0.0)]
        )
        contested, _ = extract_and_score(
            [label, value], [OcrToken("MFG 01/2027", 0, 0, 200, 30, 0.0)]
        )
        solo, _ = extract_and_score([label, value], [OcrToken("x", 0, 0, 10, 10, 0.0)])

        assert agreed["manufacturingDate"].band == ConfidenceBand.HIGH
        assert contested["manufacturingDate"].band != ConfidenceBand.HIGH
        assert solo["manufacturingDate"].band != ConfidenceBand.HIGH

    def test_a_single_engine_scan_is_not_capped(self):
        # The cap answers "two engines ran and did not agree". A PP-OCRv5-only
        # scan has no second opinion to lack, and capping it would empty the
        # HIGH band rather than describe it.
        from app.models.response_models import ConfidenceBand
        from app.services.scan_service import extract_and_score
        from app.services.ocr_service import OcrToken

        fields, _ = extract_and_score([
            OcrToken("MFG", 20, 100, 60, 30, 0.99),
            OcrToken("07/2026", 90, 100, 140, 30, 0.99),
        ])

        assert fields["manufacturingDate"].band == ConfidenceBand.HIGH


class TestFallbackTrigger:
    """When the slow engine is worth its latency.

    Measured on the reference machine: one VLM pass took 25s in isolation and
    99-120s alongside PP-OCRv5, against a 2.2s end-to-end scan on a pack
    PP-OCRv5 handles. Running it on everything would make the common case
    unusable to rescue the uncommon one.
    """

    def _settings(self, **overrides):
        from app.config import get_settings

        # vlm_trigger is pinned, not inherited. These tests assert what
        # "fallback" decides; a deployment running "always" would make them
        # pass or fail for reasons that have nothing to do with the policy.
        return get_settings().model_copy(
            update={
                "vlm_enabled": True,
                "vlm_trigger": "fallback",
                "vlm_fallback_below_core_fields": 2,
                **overrides,
            }
        )

    def _tokens(self, *texts):
        from app.services.ocr_service import OcrToken

        return [
            OcrToken(text, 20, 100 + i * 60, 300, 30, 0.97)
            for i, text in enumerate(texts)
        ]

    def test_disabled_never_runs(self):
        from app.config import get_settings
        from app.services.scan_service import should_run_vlm

        settings = get_settings().model_copy(update={"vlm_enabled": False})
        assert not should_run_vlm([], settings)

    def test_no_text_at_all_always_warrants_a_second_opinion(self):
        from app.services.scan_service import should_run_vlm

        assert should_run_vlm([], self._settings())

    def test_a_pack_the_primary_engine_read_does_not_pay_for_the_vlm(self):
        from app.services.scan_service import should_run_vlm

        tokens = self._tokens("MFG 07/2026", "EXP 06/2028", "MRP Rs. 245.00")
        assert not should_run_vlm(tokens, self._settings())

    def test_a_pack_the_primary_engine_barely_read_does(self):
        from app.services.scan_service import should_run_vlm

        # One core field out of three — the measured Eno shape, where the
        # labels read and the values did not.
        tokens = self._tokens("MRP Rs. 245.00", "Batch No.:", "Expiry Date:")
        assert should_run_vlm(tokens, self._settings())

    def test_always_runs_regardless(self):
        from app.services.scan_service import should_run_vlm

        tokens = self._tokens("MFG 07/2026", "EXP 06/2028", "MRP Rs. 245.00")
        assert should_run_vlm(tokens, self._settings(vlm_trigger="always"))

    def test_never_overrides_enabled(self):
        from app.services.scan_service import should_run_vlm

        assert not should_run_vlm([], self._settings(vlm_trigger="never"))
