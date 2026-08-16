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

    def test_tall_label_box_does_not_steal_the_next_row(self):
        """A label box spanning two printed rows must still pick its own value.

        Real geometry from a jar capture. "Mfg. Date:" was recognised as a
        238px-tall box covering its own row and part of the one below, so both
        values passed the same-line test. Horizontal distance could not tell
        them apart — the value column shares an x — and the row below won by
        ten pixels, putting the expiry date into the manufacturing field.
        """
        fields = extract_fields(
            [
                token("Mfg. Date:", 212, 142, 531, 238),
                token("1610612026", 1045, 143, 492, 134),
                token("1610112027", 1035, 274, 492, 124),
                token("Use By:", 216, 318, 416, 219),
            ]
        )

        # Slashes came back as 1s, which the normalizer repairs.
        assert fields["manufacturingDate"].normalized.value == "2026-06-16"
        assert fields["expiryDate"].normalized.value == "2027-01-16"

    def test_unit_price_never_becomes_the_mrp(self):
        """The per-unit price sits beside the MRP on nearly every pack.

        Both figures are near the MRP label, so whichever the spatial search
        reaches first wins. Returning 0.30 as the price of a 60-rupee pack is
        a confident wrong value, which section 24 treats as worse than a miss.
        """
        fields = extract_fields(
            [
                token("MRP:", 40, 100, 120, 40),
                token("Rs.0.30/g", 200, 100, 160, 40),
                token("Rs.60.00", 380, 100, 160, 40),
            ]
        )

        assert fields["mrp"].normalized.value == "60.00"

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


class TestBarePackDates:
    """Packs that stamp dates with no field names, and the guards around them.

    Every token string here came off a real TC22 capture.
    """

    def test_a_single_printed_date_is_still_recovered(self):
        # An insecticide jar stamped "MN2605121 05/26" and nothing else. The
        # pairing rule wants two dates and this pack prints one, so a value
        # read at 0.94 confidence was being thrown away for being alone.
        fields = extract_fields([
            token("MN2605121 05/26", 274, 248, 300, 39),
            token("20:17", 277, 288, 120, 44),
        ])

        assert fields["manufacturingDate"].normalized.value == "2026-05"
        # A guess about WHICH field, never about the value, so it must not be
        # presented as certain.
        assert fields["manufacturingDate"].normalized.is_ambiguous
        assert fields["manufacturingDate"].strategy == "inferred"

    def test_a_printed_time_is_not_read_as_a_date(self):
        # "20:17" is the stamp's clock. A colon is not a date separator, and
        # hour 20 would otherwise parse as a month.
        fields = extract_fields([token("20:17", 277, 288, 120, 44)])
        assert "manufacturingDate" not in fields
        assert "expiryDate" not in fields

    def test_a_lone_date_is_manufacture_even_when_it_is_in_the_future(self):
        # The convention, not a deduction: a bare stamp carries batch and
        # manufacture, or batch, manufacture and expiry, and never an expiry
        # on its own. An earlier version decided by past-or-future, which
        # happened to be right on the capture in front of it and would call a
        # long-dated pack's manufacture date an expiry.
        fields = extract_fields([token("11/2035", 274, 248)])
        assert fields["manufacturingDate"].normalized.value == "2035-11"
        assert "expiryDate" not in fields

    def test_the_batch_printed_with_the_date_is_recovered(self):
        # A stamp prints both together and names neither.
        fields = extract_fields([token("MN2605121 05/26", 274, 248, 300, 39)])
        assert fields["batchNumber"].normalized.value == "MN2605121"
        assert fields["batchNumber"].normalized.is_ambiguous

    def test_the_batch_is_found_on_the_line_beside_the_date(self):
        fields = extract_fields([
            token("05/26", 470, 191, 90, 30),
            token("MN2605121", 248, 199, 200, 30),
        ])
        assert fields["batchNumber"].normalized.value == "MN2605121"

    def test_no_batch_is_invented_without_a_date_to_anchor_to(self):
        # Off a stamp there is nothing to anchor to, and scanning a whole
        # label for anything code-shaped would adopt a licence number.
        fields = extract_fields([
            token("FT-F/2020/00002", 154, 200, 260, 30),
            token("Guwahati", 176, 183),
        ])
        assert "batchNumber" not in fields

    def test_a_date_no_product_could_carry_is_rejected(self):
        # "2.00" is a valid calendar date to the parser - February 2000 - and
        # is not a date on anything in a shop.
        fields = extract_fields([token("2.00", 300, 100)])
        assert "manufacturingDate" not in fields
        assert "expiryDate" not in fields


class TestBareDecimalNeedsCorroboration:
    """A number with two decimal places is the weakest evidence here."""

    def test_bare_decimal_is_not_a_price_on_a_pack_that_names_no_price(self):
        # The false accept this gate exists for: "92.10" sits beside
        # "USE BEFORE" on a pack carrying BATCH NO., USE BEFORE, STORAGE
        # CONDITION and NET VOL. - and no price of any kind. It is a mangled
        # date, and section 24 rates a false accept as the worst outcome here.
        fields = extract_fields([
            token("BATCH NO.", 381, 0),
            token("USE BEFORE", 381, 49),
            token("92.10", 616, 54, 90, 30, 0.67),
            token("NET VOL.", 233, 264),
        ])
        assert "mrp" not in fields

    def test_bare_decimal_is_a_price_when_the_pack_mentions_money(self):
        # The same shape on a pack that prints "M.R.P.RS:". PP-OCRv5 returned
        # the value as "F3.2.00" - the currency marker was mangled, so only
        # the bare-decimal branch can reach it.
        fields = extract_fields([
            token("M.R.P.RS:", 164, 293, 190, 32),
            token("F3.2.00", 362, 272, 160, 46, 0.75),
        ])
        assert fields["mrp"].normalized.value == "2.00"

    def test_a_mangled_word_is_not_a_mention_of_money(self):
        # "RSIY" contains RS. The stripping regex matches it, which is fine
        # for stripping and wrong for corroboration.
        fields = extract_fields([
            token("RSIY", 219, 28),
            token("92.10", 616, 54, 90, 30, 0.67),
        ])
        assert "mrp" not in fields

    def test_a_marked_price_needs_no_corroboration(self):
        fields = extract_fields([token("245.00/-", 300, 100)])
        assert fields["mrp"].normalized.value == "245.00"


class TestSpatialEvidenceIsRequired:
    """Being the only candidate is not evidence.

    Both cases here come from one capture of an Eno carton whose inkjet values
    were unreadable. With nothing legible beside the labels, the extractor
    reached across the whole pack and adopted the toll-free number printed at
    the bottom, producing a batch of 0008004420168 and an MRP of
    8,004,420,168.00 on screen.
    """

    def test_a_far_below_candidate_is_not_adopted_just_for_being_alone(self):
        fields = extract_fields([
            token("Batch No.:", 174, 78, 190, 30),
            token("0008004420168", 169, 400, 280, 30),
        ])
        assert "batchNumber" not in fields

    def test_a_price_label_does_not_adopt_a_phone_number(self):
        fields = extract_fields([
            token("M.R.P.RS:", 174, 279, 180, 30),
            token("0008004420168", 169, 400, 280, 30),
        ])
        assert "mrp" not in fields

    def test_an_implausible_amount_is_rejected_even_beside_its_label(self):
        # The magnitude gate existed only on the unlabelled path, so a labelled
        # MRP had none at all.
        fields = extract_fields([
            token("M.R.P.RS:", 174, 100, 180, 30),
            token("8004420168.00", 400, 100, 260, 30),
        ])
        assert "mrp" not in fields

    def test_a_real_price_beside_its_label_still_works(self):
        fields = extract_fields([
            token("M.R.P.RS:", 174, 100, 180, 30),
            token("245.00", 400, 100, 120, 30),
        ])
        assert fields["mrp"].normalized.value == "245.00"


class TestRunTogetherDates:
    """Two dates printed with no gap, which inkjet stamps do constantly.

    This had never worked. The substitution meant to keep the first date and
    insert a separator was written with a literal 0x01 byte where the
    backreference should have been, so every run-together stamp had its FIRST
    date destroyed and only the second survived — silently, because the result
    was still a valid date.

    It is the second time this project has lost a regex escape to the same
    mechanism; the other was a `\b` that became a backspace. Both were
    invisible in review and only showed up as accuracy.
    """

    def test_the_first_date_survives_the_split(self):
        from app.services.field_extractor import _split_run_together_dates

        assert _split_run_together_dates("22/06/2621/03/27-DB0610") == (
            "22/06/26 21/03/27-DB0610"
        )

    def test_both_dates_are_extracted_from_one_stamp(self):
        # Real geometry and real text from a capture whose manufacturing date
        # was being reported as the expiry.
        fields = extract_fields([token("22/06/2621/03/27-DB0610", 343, 222, 420, 40)])

        assert fields["manufacturingDate"].normalized.value == "2026-06-22"
        assert fields["expiryDate"].normalized.value == "2027-03-21"

    def test_no_stray_control_character_reaches_a_value(self):
        from app.services.field_extractor import _split_run_together_dates

        assert "\x01" not in _split_run_together_dates("16/06/2615/12/27")


class TestSharedDateToken:
    """Both date labels resolving to one token, with the second date unclaimed.

    Nearest-neighbour association breaks on a slightly skewed capture: the
    label column drifts down relative to the value column, so by the time the
    reader reaches "Use By" the offset to its own value exceeds the offset to
    the row above. Geometry below is from real captures.
    """

    def test_earlier_date_becomes_manufacture_when_both_labels_collide(self):
        fields = extract_fields([
            token("Mfd.:", 143, 199, 75, 39),
            token("Use By:", 144, 263, 106, 43),
            token("13/11/25", 306, 229, 242, 59),
            token("12/08/26", 327, 285, 196, 55),
        ])

        assert fields["manufacturingDate"].normalized.value == "2025-11-13"
        assert fields["expiryDate"].normalized.value == "2026-08-12"

    def test_repaired_separators_are_seen_by_the_split(self):
        # The packs this rule exists for print separators the recogniser reads
        # as digits: "16/06/2026" arrives as "1610612026". _DATE_TOKEN never
        # matches those, so the whole token has to be offered to the normalizer.
        fields = extract_fields([
            token("Mfg. Date:", 178, 167, 192, 74),
            token("Use By:", 180, 231, 148, 74),
            token("1610612026", 482, 142, 211, 62),
            token("1610112027", 481, 188, 238, 67),
        ])

        assert fields["manufacturingDate"].normalized.value == "2026-06-16"
        assert fields["expiryDate"].normalized.value == "2027-01-16"

    def test_a_stamped_time_is_not_counted_as_a_third_date(self):
        # "13:02:38" reads as a valid 2038 date and would silently disable the
        # rule by making the pack look like it printed three dates.
        fields = extract_fields([
            token("Mfd.:", 143, 199, 75, 39),
            token("Use By:", 144, 263, 106, 43),
            token("13/11/25", 306, 229, 242, 59),
            token("13:02:38", 516, 229, 201, 59),
            token("12/08/26", 327, 285, 196, 55),
        ])

        assert fields["expiryDate"].normalized.value == "2026-08-12"

    def test_three_real_dates_are_left_alone(self):
        # Nothing here says which is which, and guessing would be fabrication.
        fields = extract_fields([
            token("Mfd.:", 143, 199, 75, 39),
            token("Use By:", 144, 263, 106, 43),
            token("13/11/25", 306, 229, 242, 59),
            token("12/08/26", 327, 285, 196, 55),
            token("01/01/27", 327, 340, 196, 55),
        ])

        assert fields["manufacturingDate"].normalized.value == fields[
            "expiryDate"
        ].normalized.value


class TestCodeFieldRejections:
    def test_statutory_number_is_not_a_batch_code(self):
        # "Lic. No. 1001" shared a row with the "B.NO." label while the real
        # value sat on the row below, and was adopted as the batch number.
        fields = extract_fields([
            token("B.NO.", 260, 7, 74, 46),
            token("Lic. No. 1001", 794, 0, 166, 51),
        ])

        assert "batchNumber" not in fields

    def test_garbled_non_latin_code_is_rejected_not_tidied(self):
        # normalize_code would strip the CJK glyph and return a clean-looking
        # "LFF0359". Section 24 rates a missing code above a wrong one.
        fields = extract_fields([
            token("Batch No:", 155, 194, 166, 55),
            token("L\u4e09F.F0.359", 428, 189, 334, 46),
        ])

        assert "batchNumber" not in fields

    def test_ordinary_batch_code_still_extracts(self):
        fields = extract_fields([
            token("Batch No:", 155, 194, 166, 55),
            token("B260515", 428, 189, 334, 46),
        ])

        assert fields["batchNumber"].normalized.value == "B260515"


class TestStampBatchReachability:
    def test_batch_is_inferred_when_the_stamp_prints_two_dates(self):
        # The most common stamp layout there is. This used to sit behind an
        # early return in the two-date branch, so exactly these packs never had
        # their batch inferred.
        fields = extract_fields([
            token("SB3114C", 383, 193, 122, 54),
            token("08/10/25 07/10/26", 500, 183, 251, 63),
        ])

        assert fields["batchNumber"].normalized.value == "SB3114C"

    def test_footnote_marker_is_not_part_of_the_code(self):
        # A pack tying label to value with "#" and "*" delivers "#RC-DS5720",
        # which failed the very first character of the stamp-code pattern.
        fields = extract_fields([
            token("#RC-DS5720", 313, 322, 273, 44),
            token("*07.2025", 311, 365, 233, 43),
        ])

        assert fields["batchNumber"].normalized.value == "RC-DS5720"


class TestMisreadSlashPrice:
    def test_rupee_slash_dash_read_as_one_still_yields_a_price(self):
        # "190/-" comes back as "1901-" on a dot-matrix stamp: the same
        # slash-as-1 confusion the date repair exists for.
        fields = extract_fields([
            token("MRP", 100, 150, 80, 40),
            token("1901-30.251g", 163, 200, 207, 55),
        ])

        assert fields["mrp"].normalized.value == "190.00"
