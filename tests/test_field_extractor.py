"""Field extraction and spatial association tests (sections 8 and 9)."""

from __future__ import annotations

from app.services.field_extractor import extract_fields
from app.services.ocr_service import OcrToken


def token(text: str, x: int, y: int, width: int = 120, height: int = 30, conf: float = 0.97):
    return OcrToken(text, x, y, width, height, conf)


class TestSameLineAssociation:
    def test_value_to_the_right_of_label(self):
        fields = extract_fields([token("BATCH NO", 20, 100, 180), token("A23C91", 300, 100)])

        assert fields["batchNumber"].normalized.value == "A23C91"
        assert fields["batchNumber"].strategy == "same_line"

    def test_ignores_value_to_the_left_of_label(self):
        # Reading order matters; a value printed before its label is not ours.
        fields = extract_fields([token("A23C91", 20, 100), token("BATCH NO", 300, 100, 180)])
        assert "batchNumber" not in fields

    def test_small_vertical_offset_still_counts_as_same_line(self):
        fields = extract_fields([token("BATCH NO", 20, 100, 180), token("A23C91", 300, 106)])
        assert fields["batchNumber"].normalized.value == "A23C91"

    def test_nearest_plausible_candidate_wins(self):
        fields = extract_fields(
            [token("BATCH NO", 20, 100, 180), token("A23C91", 300, 100), token("ZZ999", 900, 100)]
        )
        assert fields["batchNumber"].normalized.value == "A23C91"


class TestBelowAssociation:
    def test_value_beneath_label(self):
        fields = extract_fields([token("BATCH NO", 20, 100, 180), token("A23C91", 24, 140)])

        assert fields["batchNumber"].normalized.value == "A23C91"
        assert fields["batchNumber"].strategy == "below"

    def test_same_line_preferred_over_below(self):
        fields = extract_fields(
            [token("BATCH NO", 20, 100, 180), token("A23C91", 300, 100), token("BB888", 24, 140)]
        )

        assert fields["batchNumber"].normalized.value == "A23C91"
        assert fields["batchNumber"].strategy == "same_line"


class TestInlineAssociation:
    def test_label_and_value_in_one_token(self):
        fields = extract_fields([token("BATCH NO A23C91", 20, 100, 300)])

        assert fields["batchNumber"].normalized.value == "A23C91"
        assert fields["batchNumber"].strategy == "inline"

    def test_inline_date(self):
        fields = extract_fields([token("MFG 07/26", 20, 100, 200)])
        assert fields["manufacturingDate"].normalized.value == "2026-07"

    def test_inline_price(self):
        fields = extract_fields([token("MRP Rs. 245.00", 20, 100, 300)])
        assert fields["mrp"].normalized.value == "245.00"


class TestAliases:
    def test_punctuation_and_spacing_are_ignored(self):
        for label in ["BATCH NO.", "B.NO.", "b no", "BNO"]:
            fields = extract_fields([token(label, 20, 100, 180), token("A23C91", 300, 100)])
            assert fields["batchNumber"].normalized.value == "A23C91", label

    def test_longer_alias_wins(self):
        # "BATCH NO" must be preferred over the shorter "BATCH".
        fields = extract_fields([token("BATCH NO", 20, 100, 180), token("A23C91", 300, 100)])
        assert fields["batchNumber"].label_match_quality == 1.0

    def test_mrp_aliases(self):
        for label in ["MRP", "M.R.P.", "MAX RETAIL PRICE"]:
            fields = extract_fields([token(label, 20, 100, 180), token("245.00", 300, 100)])
            assert fields["mrp"].normalized.value == "245.00", label

    def test_expiry_aliases(self):
        for label in ["EXP", "EXPIRY", "USE BEFORE", "BEST BEFORE"]:
            fields = extract_fields([token(label, 20, 100, 180), token("06/28", 300, 100)])
            assert fields["expiryDate"].normalized.value == "2028-06", label


class TestPlausibilityGate:
    def test_label_never_becomes_another_labels_value(self):
        # Without the gate, "MFG" adopts the neighbouring "EXP" token.
        fields = extract_fields([token("MFG", 20, 100, 80), token("EXP", 200, 100, 80)])
        assert "manufacturingDate" not in fields

    def test_date_field_rejects_a_code(self):
        fields = extract_fields([token("MFG", 20, 100, 80), token("A23C91", 300, 100)])
        assert "manufacturingDate" not in fields

    def test_currency_field_rejects_pure_text(self):
        fields = extract_fields([token("MRP", 20, 100, 80), token("SPECIAL", 300, 100)])
        assert "mrp" not in fields

    def test_code_field_rejects_short_noise(self):
        fields = extract_fields([token("LOT", 20, 100, 80), token("-", 300, 100)])
        assert "lotCode" not in fields


class TestMissingFields:
    def test_absent_label_yields_no_candidate(self):
        fields = extract_fields([token("SOME PRODUCT NAME", 20, 100, 400)])
        assert fields == {}

    def test_label_with_no_plausible_value_yields_nothing(self):
        # Never invent a value just because the label was found.
        fields = extract_fields([token("BATCH NO", 20, 100, 180)])
        assert "batchNumber" not in fields


class TestDerivedExpiry:
    def test_derives_expiry_from_shelf_life(self):
        fields = extract_fields(
            [
                token("MFG 03/2026", 20, 80, 200),
                token("BEST BEFORE 6 MONTHS FROM PACKAGING", 20, 120, 460),
            ]
        )

        expiry = fields["expiryDate"]
        assert expiry.normalized.value == "2026-09"
        assert expiry.strategy == "derived"
        assert expiry.normalized.is_ambiguous, "must land in operator review"

    def test_printed_expiry_beats_derivation(self):
        fields = extract_fields(
            [
                token("MFG 03/2026", 20, 80, 200),
                token("EXP", 20, 120, 70),
                token("12/2027", 300, 120),
                token("BEST BEFORE 6 MONTHS FROM PACKAGING", 20, 160, 460),
            ]
        )

        assert fields["expiryDate"].normalized.value == "2027-12"
        assert fields["expiryDate"].strategy != "derived"

    def test_no_derivation_without_a_manufacturing_date(self):
        fields = extract_fields([token("BEST BEFORE 6 MONTHS FROM PACKAGING", 20, 120, 460)])
        assert "expiryDate" not in fields


class TestRealLabelLayouts:
    def test_columnar_label(self):
        fields = extract_fields(
            [
                token("BATCH NO", 40, 140, 200, 40), token("A23C91", 300, 140, 140, 40),
                token("MFG", 40, 230, 80, 40), token("07/26", 300, 230, 120, 40),
                token("EXP", 40, 320, 70, 40), token("06/28", 300, 320, 120, 40),
                token("LOT", 40, 410, 70, 40), token("K18P2", 300, 410, 120, 40),
                token("MRP Rs. 245.00", 40, 510, 300, 40),
            ]
        )

        assert {k: v.normalized.value for k, v in fields.items()} == {
            "batchNumber": "A23C91",
            "manufacturingDate": "2026-07",
            "expiryDate": "2028-06",
            "lotCode": "K18P2",
            "mrp": "245.00",
        }

    def test_snack_pouch_layout(self):
        fields = extract_fields(
            [
                token("MFG 03/2026", 20, 80, 200),
                token("BEST BEFORE 6 MONTHS FROM PACKAGING", 20, 120, 460),
                token("BATCH B.NO KX7741", 20, 160, 260),
                token("M.R.P. Rs. 20.00 (incl. of all taxes)", 20, 200, 470),
            ]
        )

        assert fields["batchNumber"].normalized.value == "KX7741"
        assert fields["manufacturingDate"].normalized.value == "2026-03"
        assert fields["expiryDate"].normalized.value == "2026-09"
        assert fields["mrp"].normalized.value == "20.00"
        assert "lotCode" not in fields, "this pack has no separate lot code"
