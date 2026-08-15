"""Normalization tests (CLAUDE.md section 10).

Date parsing is called out in section 28 as requiring unit tests, because it
is where a silent misread turns into a wrong expiry date on a printed label.
"""

from __future__ import annotations

import pytest

from app.services import normalizer


class TestMonthYearDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("07/26", "2026-07"),
            ("07-26", "2026-07"),
            ("07/2026", "2026-07"),
            ("07-2026", "2026-07"),
            ("07.2026", "2026-07"),
            ("0726", "2026-07"),
            ("072026", "2026-07"),
            ("12/99", "2099-12"),
            ("01/00", "2000-01"),
        ],
    )
    def test_month_year_forms(self, raw, expected):
        assert normalizer.normalize_date(raw).value == expected

    def test_label_prefix_is_stripped(self):
        assert normalizer.normalize_date("MFG 07/26").value == "2026-07"
        assert normalizer.normalize_date("EXP: 06/28").value == "2028-06"

    @pytest.mark.parametrize("raw", ["13/26", "00/26", "99/26"])
    def test_month_out_of_range_is_rejected(self, raw):
        assert normalizer.normalize_date(raw).value is None


class TestFullDates:
    def test_unambiguous_day_first(self):
        # 25 cannot be a month, so this reading is forced.
        result = normalizer.normalize_date("25/07/2026")
        assert result.value == "2026-07-25"
        assert not result.is_ambiguous

    def test_unambiguous_month_first(self):
        # 25 cannot be a month, so it must be the day.
        result = normalizer.normalize_date("07/25/2026")
        assert result.value == "2026-07-25"
        assert not result.is_ambiguous

    def test_genuinely_ambiguous_is_flagged_not_rejected(self):
        # 07/07 reads the same either way here, but 03/04 does not.
        result = normalizer.normalize_date("03/04/2026")
        assert result.value == "2026-04-03", "Indian convention is day-first"
        assert result.is_ambiguous, "must be flagged so confidence drops"
        assert result.notes

    def test_invalid_calendar_date_is_rejected(self):
        assert normalizer.normalize_date("31/02/2026").value is None

    def test_two_digit_year_in_full_date(self):
        assert normalizer.normalize_date("25/07/26").value == "2026-07-25"


class TestNamedMonths:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("JAN 2026", "2026-01"),
            ("JAN26", "2026-01"),
            ("MAR 26", "2026-03"),
            ("DECEMBER 2026", "2026-12"),
            ("SEP 2026", "2026-09"),
        ],
    )
    def test_named_month_year(self, raw, expected):
        assert normalizer.normalize_date(raw).value == expected

    def test_named_month_with_day(self):
        assert normalizer.normalize_date("12 MAR 2026").value == "2026-03-12"


class TestDateRejection:
    @pytest.mark.parametrize("raw", ["", "   ", "BATCH", "A23C91", "abc"])
    def test_non_dates_return_none(self, raw):
        assert normalizer.normalize_date(raw).value is None

    def test_never_fabricates_on_partial_input(self):
        # A bare month with no year must not invent one.
        assert normalizer.normalize_date("JAN").value is None


class TestOcrArtifactRepair:
    """Damage the recogniser does to a date that is otherwise readable.

    Every string here was returned by PP-OCRv5 on a real ROI capture.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Slashes recognised as the digit 1: "16/06/2026" came back as
            # "1610612026" on two separate captures.
            ("1610612026", "2026-06-16"),
            ("1610112027", "2027-01-16"),
            # Two-digit year form.
            ("1610612 6".replace(" ", ""), "2026-06-16"),
        ],
    )
    def test_slash_recognised_as_one_is_repaired(self, raw, expected):
        assert normalizer.normalize_date(raw, "day").value == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "9876543210",   # phone number
            "1234512345",   # month 34 is not a month
            "4010612026",   # day 40 is not a day
        ],
    )
    def test_repair_does_not_fire_on_non_dates(self, raw):
        # The repair must not manufacture a date out of a licence or phone
        # number that happens to be ten digits long.
        assert normalizer.normalize_date(raw, "day").value is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # A lot code running into the expiry date, and a label fragment.
            ("L409/06/2027", "2027-06-09"),
            ("B.05/08/2027", "2027-08-05"),
        ],
    )
    def test_letter_prefix_is_stripped(self, raw, expected):
        assert normalizer.normalize_date(raw, "day").value == expected


class TestCurrency:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("245.00", "245.00"),
            ("MRP Rs. 245.00", "245.00"),
            ("Rs 245/-", "245.00"),
            ("₹1,245.50", "1245.50"),
            ("MRP:245", "245.00"),
            ("M.R.P. Rs. 20.00", "20.00"),
        ],
    )
    def test_common_printings(self, raw, expected):
        assert normalizer.normalize_currency(raw).value == expected

    def test_words_do_not_become_numbers(self):
        # "incl. of all taxes" contains I, O and S. Translating those to
        # digits would invent a price out of ordinary text.
        result = normalizer.normalize_currency("M.R.P. Rs. 20.00 (incl. of all taxes)")
        assert result.value == "20.00"
        assert not result.is_ambiguous

    def test_price_preferred_over_pack_weight(self):
        # "MRP Rs 20/- per 52g" — anchor to the currency marker, not length.
        result = normalizer.normalize_currency("MRP Rs 20/- per 52g")
        assert result.value == "20.00"
        assert result.is_ambiguous, "two numbers present; operator should confirm"

    @pytest.mark.parametrize("raw", ["", "MRP", "Rs.", "abc", "0", "0.00"])
    def test_rejects_non_prices(self, raw):
        assert normalizer.normalize_currency(raw).value is None

    def test_leading_dash_is_treated_as_a_separator(self):
        # Printed prices are never negative. A leading dash on a label is a
        # separator or an OCR artifact, so the digits still read as the price.
        assert normalizer.normalize_currency("-5").value == "5.00"


class TestCode:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("A23C91", "A23C91"),
            ("  k18p2  ", "K18P2"),
            ("BATCH A23C91", "A23C91"),
            ("B.NO KX7741", "KX7741"),
            ("LOT: K18P2", "K18P2"),
            ("AB-123/C", "AB-123/C"),
        ],
    )
    def test_code_normalization(self, raw, expected):
        assert normalizer.normalize_code(raw).value == expected

    def test_letter_o_is_never_forced_to_zero(self):
        # Section 10 forbids blind global replacement. A batch code really can
        # contain the letter O, and corrupting a correct code is worse than
        # surfacing an uncertain one.
        assert normalizer.normalize_code("BATCH OO12").value == "OO12"

    def test_letter_i_and_s_are_preserved(self):
        assert normalizer.normalize_code("ISB123").value == "ISB123"

    @pytest.mark.parametrize("raw", ["", "   ", "---", "..."])
    def test_rejects_empty_codes(self, raw):
        assert normalizer.normalize_code(raw).value is None


class TestRelativePeriod:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("BEST BEFORE 9 MONTHS FROM PACKAGING", (9, "MONTH")),
            ("Best before 6 months from the date of manufacture", (6, "MONTH")),
            ("BEST BEFORE NINE MONTHS FROM MFG", (9, "MONTH")),
            ("USE WITHIN 30 DAYS OF OPENING", (30, "DAY")),
            ("BEST BEFORE 2 YEARS FROM MFG", (2, "YEAR")),
        ],
    )
    def test_parses_shelf_life(self, raw, expected):
        assert normalizer.parse_relative_period(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["EXP 06/28", "BATCH A23C91", "", "BEST QUALITY GUARANTEED"]
    )
    def test_ignores_text_without_a_period(self, raw):
        assert normalizer.parse_relative_period(raw) is None


class TestAddPeriod:
    def test_adds_months_within_year(self):
        assert normalizer.add_period("2026-03", 6, "MONTH") == "2026-09"

    def test_adds_months_across_year_boundary(self):
        assert normalizer.add_period("2026-09", 6, "MONTH") == "2027-03"

    def test_adds_years(self):
        assert normalizer.add_period("2026-03", 2, "YEAR") == "2028-03"

    def test_preserves_day_precision(self):
        assert normalizer.add_period("2026-01-15", 2, "MONTH") == "2026-03-15"

    def test_clamps_day_to_short_month(self):
        assert normalizer.add_period("2026-01-31", 1, "MONTH") == "2026-02-28"

    def test_month_precision_input_yields_month_precision_output(self):
        # Deriving a day from a pack that never printed one is fabrication.
        assert normalizer.add_period("2026-03", 30, "DAY") is None

    def test_adds_days_when_day_is_known(self):
        assert normalizer.add_period("2026-03-01", 30, "DAY") == "2026-03-31"
