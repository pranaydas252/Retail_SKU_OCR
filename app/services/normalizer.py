"""Value normalization (CLAUDE.md section 10).

Turns raw OCR text into consistent server-side formats:

    dates     YYYY-MM for month/year, YYYY-MM-DD for full dates
    currency  decimal string, two places
    code      significant alphanumerics, preserved

Two rules govern everything here:

1. Never fabricate. If a value cannot be justified, return None and let the
   operator supply it. A missing field is recoverable; a wrong expiry date is
   the failure this project exists to avoid.
2. Never silently resolve ambiguity. A date that could read two ways is
   resolved under a stated convention AND flagged, so confidence drops and the
   operator sees a review prompt.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date

# OCR confusions from CLAUDE.md section 10, applied ONLY where the field
# format makes the correction defensible — that is, in positions that must be
# a digit. Applying these globally would corrupt genuine codes: a batch number
# really can contain the letter O.
_DIGIT_CONFUSIONS = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "S": "5", "s": "5",
    "B": "8",
    "G": "6",
    "Z": "2", "z": "2",
})

_MONTH_NAMES = {
    name.upper(): index
    for index, name in enumerate(calendar.month_abbr)
    if name
} | {
    name.upper(): index
    for index, name in enumerate(calendar.month_name)
    if name
}

# Two-digit years on retail labels are 2000s. A 1990s SKU label is not in
# scope, so a pivot window would add ambiguity without buying anything.
_CENTURY = 2000

#: Any month name or abbreviation, longest first so "JANUARY" wins over "JAN".
#: Exported because the extractor needs to tell a date wrapped in letters from a
#: code that merely contains digits.
MONTH_NAME_PATTERN = re.compile(
    "|".join(sorted((re.escape(n) for n in _MONTH_NAMES), key=len, reverse=True)),
    re.IGNORECASE,
)

# Output format of this module, recognized on input so normalization is
# idempotent.
_ISO_INPUT = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


@dataclass
class NormalizedValue:
    """Result of normalizing one raw string."""

    value: str | None
    is_ambiguous: bool = False
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None


def _digits_only(text: str) -> str:
    """Coerce a token that must be numeric, applying OCR confusion fixes."""
    return re.sub(r"[^0-9]", "", text.translate(_DIGIT_CONFUSIONS))


# A run of digits and digit-confusable letters. Used to find numbers inside
# free text without letting ordinary words become numbers.
_NUMERIC_RUN = re.compile(r"[0-9OoQDIlisSBGZz|]+(?:\.[0-9OoQDIlisSBGZz|]+)?")


def _numeric_runs(text: str) -> list[str]:
    """Extract numeric values from free text.

    Confusion correction is applied ONLY inside runs that already contain a
    real digit. Translating the whole string would manufacture numbers out of
    ordinary words: "(incl. of all taxes)" contains I, O and S, which would
    otherwise become 1, 0 and 5 and be read as a price.
    """
    results = []
    for run in _NUMERIC_RUN.findall(text):
        if not any(char.isdigit() for char in run):
            continue
        converted = run.translate(_DIGIT_CONFUSIONS)
        for number in re.findall(r"\d+(?:\.\d+)?", converted):
            results.append(number)
    return results


def _resolve_year(raw: str) -> int | None:
    digits = _digits_only(raw)
    if len(digits) == 4:
        year = int(digits)
        return year if 1990 <= year <= 2099 else None
    if len(digits) == 2:
        return _CENTURY + int(digits)
    return None


def _month_from_name(text: str) -> int | None:
    cleaned = re.sub(r"[^A-Za-z]", "", text).upper()
    if not cleaned:
        return None
    if cleaned in _MONTH_NAMES:
        return _MONTH_NAMES[cleaned]
    # "SEPT" and similar abbreviations that calendar does not carry.
    for name, index in _MONTH_NAMES.items():
        if len(name) > 3 and name.startswith(cleaned) and len(cleaned) >= 3:
            return index
    return None


#: A date whose separators were recognised as a digit.
#:
#: On thin inkjet and dot-matrix print a forward slash is nearly the same mark
#: as a "1", and on a slanted dot-matrix stamp it is nearly the same mark as a
#: "7". The recogniser returns "16/06/2026" as "1610612026", and — measured on
#: a TC22 capture of a foil pack printing "EXP.10/2026" — returns "1072026".
#:
#: The digits either side are all correct; only the separator is wrong. But a
#: greedy date match reads the whole run as a licence number and gives up, so
#: the field is reported as never recognised. Three captures were lost this
#: way before the repair existed, and one after it, because it only knew
#: about "1".
#:
#: Deliberately narrow. The substituted digit must sit exactly where a
#: separator belongs, both separators in a three-part date must be the SAME
#: misread character — one glyph, one systematic error — and every component
#: either side must be a real day, month and year. A phone number or a batch
#: code does not satisfy all of those at once.
_SLASH_AS_DIGIT_DMY = re.compile(r"^(\d{2})([17])(\d{2})\2(\d{2}|\d{4})$")

#: The same failure on a two-part MM/YYYY stamp, which is what Indian FMCG
#: packs print most often. "10/2026" -> "1072026", "11/2024" -> "1112024".
#:
#: The year must be four digits and this century. A two-digit year would make
#: the pattern five characters long and match far too much.
#: The same failure on only ONE of the two separators.
#:
#: Measured: a pack printing "27/06/2026" came back as "27/0672026" - the first
#: slash survived and the second became a 7. The rule above cannot see that,
#: because it requires both separators to be the SAME misread character.
#:
#: A real separator must survive, and that requirement is what keeps this safe.
#: Dropping it would match "2710672026", which is equally a phone number and a
#: date with nothing in the digits to say which. One surviving slash is the
#: evidence that a date was printed there at all.
_SLASH_PARTLY_READ_AS_DIGIT = re.compile(
    r"^(\d{1,2})(?:([/.\-])(\d{1,2})([17])|([17])(\d{1,2})([/.\-]))(\d{2}|\d{4})$"
)

_SLASH_AS_DIGIT_MY = re.compile(r"^(\d{2})([17])(\d{4})$")

#: A complete day-month-year date, wherever it sits in a noisy token.
#:
#: Inkjet stamps run fields together, so the recogniser returns the expiry
#: welded to whatever preceded it: "L409/06/2027" is a lot-code tail followed
#: by 09/06/2027, and "B.05/08/2027" is a label fragment followed by
#: 05/08/2027. Parsing the whole token yields nonsense or nothing.
#:
#: Only used when the token does not already begin with a digit, so a
#: well-formed date is never re-cut, and only for complete three-part dates —
#: a partial match would let this invent a day that was not printed.
_EMBEDDED_DATE = re.compile(
    r"\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{4}"
    r"|\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2}"
)


def _repair_slash_read_as_digit(text: str) -> str:
    """Put the separators back into a date whose slashes were read as digits.

    Handles DD?MM?YYYY and MM?YYYY, where ? is a 1 or a 7. Returns the text
    unchanged when the components either side are not a real date, which is
    what keeps a batch code or a licence number from being rewritten into one.
    """
    match = _SLASH_AS_DIGIT_DMY.match(text)
    if match:
        day, _, month, year = match.groups()
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            return f"{day}/{month}/{year}"
        return text

    match = _SLASH_PARTLY_READ_AS_DIGIT.match(text)
    if match:
        day, _, month_a, _, _, month_b, _, year = match.groups()
        month = month_a or month_b
        if month and 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            return f"{day}/{month}/{year}"
        return text

    match = _SLASH_AS_DIGIT_MY.match(text)
    if match:
        month, _, year = match.groups()
        # A month out of range, or a year outside this century, means the run
        # is something else that happens to be seven digits long.
        if 1 <= int(month) <= 12 and 2000 <= int(year) <= 2099:
            return f"{month}/{year}"
        return text

    return text


def _strip_leading_junk(text: str) -> str:
    """Cut a clean date out of a token that starts with something else."""
    if not text or text[0].isdigit():
        return text
    found = _EMBEDDED_DATE.search(text)
    return found.group(0) if found else text


def normalize_date(raw: str, precision: str = "month") -> NormalizedValue:
    """Normalize a printed date.

    precision "month" prefers YYYY-MM, "day" prefers YYYY-MM-DD. Either way the
    output matches what was actually printed: a label showing only MM/YY cannot
    yield a day, and one showing DD/MM/YYYY is not truncated.

    Ambiguity: for DD/MM/YY vs MM/DD/YY, Indian retail convention is
    day-first, so that reading is used. When both readings are valid — both
    leading numbers <= 12 — the result is flagged ambiguous, which costs
    confidence and pushes the field into operator review.
    """
    if not raw or not raw.strip():
        return NormalizedValue(None, notes=["empty"])

    text = raw.strip().upper()

    # Already-normalized input passes straight through. Normalization must be
    # idempotent because /confirm re-normalizes what the app sends back, and
    # the app sends back the ISO value the server produced. Without this,
    # "2026-07" is re-parsed as month 2026 and every confirmed scan picks up a
    # spurious "month out of range" note in its audit trail.
    already_iso = _ISO_INPUT.match(text)
    if already_iso:
        year = int(already_iso.group(1))
        month = int(already_iso.group(2))
        day = int(already_iso.group(3)) if already_iso.group(3) else None

        if not 1 <= month <= 12:
            return NormalizedValue(None, notes=["month out of range"])
        if day is not None:
            if not _valid_day(year, month, day):
                return NormalizedValue(None, notes=["invalid calendar date"])
            return NormalizedValue(f"{year:04d}-{month:02d}-{day:02d}")
        return NormalizedValue(f"{year:04d}-{month:02d}")

    # Strip a leading label fragment the extractor may have carried along.
    text = re.sub(r"^(MFG|MFD|EXP|EXPIRY|PKD|PACKED|USE BEFORE|BEST BEFORE)[.:\s]*", "", text)
    text = text.strip(" .:-–—")

    text = _repair_slash_read_as_digit(text)
    text = _strip_leading_junk(text)

    named = _try_named_month(text)
    if named is not None:
        return named

    parts = [p for p in re.split(r"[^0-9A-Z]+", text) if p]
    if not parts:
        return NormalizedValue(None, notes=["no date components"])

    if len(parts) >= 3:
        return _three_part_date(parts[:3], precision)
    if len(parts) == 2:
        return _two_part_date(parts)
    return _single_part_date(parts[0])


def _try_named_month(text: str) -> NormalizedValue | None:
    """Handle 'JAN 2026', 'JAN26', '12 MAR 2026'."""
    # Every alphabetic run, not just the first. Indian packs print
    # "Mfg.Month&Year:JAN.2025", where the first run is "MONTH" and the month
    # name is the second - giving up on the first run reported no date at all
    # for a pack that states it plainly.
    month = None
    for candidate in re.findall(r"([A-Z]{3,9})", text):
        month = _month_from_name(candidate)
        if month is not None:
            break
    if month is None:
        return None

    numbers = [n for n in re.findall(r"\d+", text)]
    if not numbers:
        return NormalizedValue(None, notes=["month name without year"])

    if len(numbers) == 1:
        year = _resolve_year(numbers[0])
        if year is None:
            return NormalizedValue(None, notes=["unreadable year"])
        return NormalizedValue(f"{year:04d}-{month:02d}")

    day_raw, year_raw = (numbers[0], numbers[-1])
    year = _resolve_year(year_raw)
    day = int(_digits_only(day_raw) or 0)
    if year is None:
        return NormalizedValue(None, notes=["unreadable year"])
    if not _valid_day(year, month, day):
        return NormalizedValue(f"{year:04d}-{month:02d}", notes=["day out of range, kept month"])
    return NormalizedValue(f"{year:04d}-{month:02d}-{day:02d}")


def _three_part_date(parts: list[str], precision: str) -> NormalizedValue:
    """DD/MM/YYYY or MM/DD/YYYY."""
    first, second, year_raw = parts
    year = _resolve_year(year_raw)
    if year is None:
        return NormalizedValue(None, notes=["unreadable year"])

    a = int(_digits_only(first) or -1)
    b = int(_digits_only(second) or -1)
    if a < 0 or b < 0:
        return NormalizedValue(None, notes=["unreadable day/month"])

    ambiguous = False
    if a > 12 and 1 <= b <= 12:
        day, month = a, b            # unambiguous: first cannot be a month
    elif b > 12 and 1 <= a <= 12:
        day, month = b, a            # unambiguous: second cannot be a month
    elif 1 <= a <= 12 and 1 <= b <= 12:
        day, month = a, b            # both plausible; Indian convention is day-first
        ambiguous = True
    else:
        return NormalizedValue(None, notes=["day/month out of range"])

    if not _valid_day(year, month, day):
        return NormalizedValue(None, notes=["invalid calendar date"])

    notes = ["day-first assumed (DD/MM); MM/DD also valid"] if ambiguous else []

    if precision == "month":
        # The label printed a full date; keep it rather than discarding detail.
        return NormalizedValue(f"{year:04d}-{month:02d}-{day:02d}", ambiguous, notes)
    return NormalizedValue(f"{year:04d}-{month:02d}-{day:02d}", ambiguous, notes)


def _two_part_date(parts: list[str]) -> NormalizedValue:
    """MM/YY or MM/YYYY — the common SKU label form."""
    first, second = parts
    year = _resolve_year(second)
    if year is None:
        return NormalizedValue(None, notes=["unreadable year"])

    month = int(_digits_only(first) or -1)
    if not 1 <= month <= 12:
        return NormalizedValue(None, notes=["month out of range"])

    return NormalizedValue(f"{year:04d}-{month:02d}")


def _single_part_date(part: str) -> NormalizedValue:
    """MMYY or MMYYYY printed without a separator."""
    digits = _digits_only(part)

    if len(digits) == 4:
        month, year = int(digits[:2]), _CENTURY + int(digits[2:])
    elif len(digits) == 6:
        month, year = int(digits[:2]), int(digits[2:])
    else:
        return NormalizedValue(None, notes=["unrecognized date format"])

    if not 1 <= month <= 12:
        return NormalizedValue(None, notes=["month out of range"])
    if not 1990 <= year <= 2099:
        return NormalizedValue(None, notes=["year out of range"])

    return NormalizedValue(f"{year:04d}-{month:02d}")


def _valid_day(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


#: A per-unit price. Never the MRP, and the single most common wrong answer
#: for that field on an Indian retail pack.
#:
#: Written against what the recogniser returns rather than how the pack is
#: printed. Measured across the ROI captures, the unit price came back as
#: "0.30/g", "0.35/9", "0.43/9", "0.45/m", "(30.451g)" and welded to the
#: retail price as "250.00(0.31/9)". Both halves of the "/g" need to tolerate
#: confusion:
#:
#: * the separator "/" is read as "1" or "|" — "30.451g" is "30.45/g"
#: * the unit "g" is read as "9" or "q", and "ml" is truncated to "m"
#:
#: Requiring a literal "/g" matched two of those six. Exported because the
#: field extractor needs the identical judgement when it decides whether a
#: token beside the MRP label is a candidate at all; two copies of this rule
#: drifted apart once already.
#: The two confusions must never both be loose at the same time. Allowing a
#: "1" separator together with a "9" unit made this match 3-1-9 inside
#: "319.00" and delete the retail price it was meant to protect. So: a real
#: slash may be followed by a confused unit, and a confused slash must be
#: followed by a genuine letter.
PER_UNIT_PRICE = re.compile(
    r"[0-9OoQDIlisSBGZz.,]+\s*(?:/|PER\s*)\s*(?:GM|GRAM|ML|KG|PCS|PC|[G9QML])\b"
    r"|[0-9OoQDIlisSBGZz.,]+\s*[1|]\s*(?:GM|GRAM|ML|KG|PCS|PC|[GQML])\b",
    re.IGNORECASE,
)


def normalize_currency(raw: str) -> NormalizedValue:
    """Normalize an MRP into a two-decimal string.

    Handles the common Indian printings: 'MRP Rs. 245.00', 'Rs 245/-',
    '₹1,245.50', 'MRP:245'.
    """
    if not raw or not raw.strip():
        return NormalizedValue(None, notes=["empty"])

    text = raw.upper().replace(",", "")

    # Indian FMCG packs routinely print the price beside other numbers:
    #   "MRP Rs 20/- per 52g"      "M.R.P. Rs. 20.00 (incl. of all taxes)"
    # so take the number that follows the currency or MRP marker in
    # preference to the longest number anywhere in the string.
    anchored = re.search(
        r"(?:M\.?R\.?P\.?|MAX(?:IMUM)?\s*RETAIL\s*PRICE|RS\.?|INR|₹)\s*:?\s*"
        r"([0-9OoQDIlisSBGZz|]+(?:\.[0-9OoQDIlisSBGZz|]+)?)",
        text,
    )

    stripped = re.sub(r"(MRP|M\.?R\.?P\.?|MAX(IMUM)?\s*RETAIL\s*PRICE)", " ", text)
    stripped = re.sub(r"(RS\.?|INR|₹|/-|/=)", " ", stripped)

    # Strip per-unit prices before looking for the MRP. "Rs.60.00 Rs.0.30/g"
    # and "180.00 @ 0.72/ml" both print a unit price beside the retail price,
    # and it was being picked instead — the extractor returned 72.00 for a
    # bottle marked 180.00.
    # The recogniser also welds the two prices into a single token —
    # "250.00(0.31/9)" — so the unit price has to be removed before the
    # numbers are pulled out, not filtered afterwards.
    stripped = PER_UNIT_PRICE.sub(" ", stripped)
    stripped = re.sub(r"\bUSP\b.*", " ", stripped)

    matches = _numeric_runs(stripped)

    if anchored:
        anchored_value = re.sub(
            r"[^0-9.]", "", anchored.group(1).translate(_DIGIT_CONFUSIONS)
        ).strip(".")
        if anchored_value:
            matches = [anchored_value] + [m for m in matches if m != anchored_value]

    if not matches:
        return NormalizedValue(None, notes=["no numeric value"])

    # Anchored match first if we found one, else longest wins: '245.00' beats
    # a stray '2' picked up from surrounding text.
    best = matches[0] if anchored else max(matches, key=len)
    try:
        amount = float(best)
    except ValueError:
        return NormalizedValue(None, notes=["unparseable amount"])

    if amount <= 0:
        return NormalizedValue(None, notes=["non-positive price"])

    notes = ["multiple numbers present"] if len(matches) > 1 else []
    return NormalizedValue(f"{amount:.2f}", is_ambiguous=bool(notes), notes=notes)


def normalize_code(raw: str) -> NormalizedValue:
    """Normalize a batch or lot code.

    Deliberately does NOT apply OCR confusion correction. A batch code has no
    known format, so 'O' cannot be assumed to be a zero — CLAUDE.md section 10
    forbids blind global replacement, and corrupting a correct code is worse
    than surfacing an uncertain one. The operator confirms it.

    Only surrounding punctuation and separators are stripped.
    """
    if not raw or not raw.strip():
        return NormalizedValue(None, notes=["empty"])

    text = raw.strip().upper()

    # Repeatedly, not once: a pack that prints "Lot / Batch No. MRDTW..." has
    # two label words in front of the value, and stripping one left "BATCHNO"
    # glued to the code.
    label = re.compile(
        r"^\s*(?:LOT|BATCH|B\.?\s*NO\.?|BN|NO\.?|CODE|NUMBER)\s*[./:\-]*\s*"
    )
    while True:
        stripped = label.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped

    # Internal spacing is kept, because it is printed.
    #
    # A pack reading "Lot No.: M2 ZX2626PZAB" was stored and printed as
    # "M2ZX2626PZAB", so the label in the operator's hand and the label coming
    # out of the printer disagreed about the code. The operator is checking one
    # against the other; making them differ is the one thing this value must
    # not do.
    #
    # Safe because nothing downstream compares codes literally. Engine
    # agreement (ensemble.same_value) and the accuracy harness both compare
    # alphanumeric skeletons, so a space can neither manufacture a
    # disagreement nor fail a match. Runs are collapsed and the ends trimmed,
    # so a stray recognition space cannot pad the value.
    cleaned = re.sub(r"[^A-Z0-9\-/ ]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -/")

    if not cleaned:
        return NormalizedValue(None, notes=["no alphanumeric content"])
    if not re.search(r"[A-Z0-9]", cleaned):
        return NormalizedValue(None, notes=["no significant characters"])

    return NormalizedValue(cleaned)


# Indian FMCG frequently prints a shelf life instead of an expiry date:
#   "BEST BEFORE 9 MONTHS FROM PACKAGING"
#   "BEST BEFORE NINE MONTHS FROM THE DATE OF MANUFACTURE"
#   "USE WITHIN 6 MONTHS OF MFG"
# There is no expiry date on the pack to read — it has to be derived from the
# manufacturing date. Common on snacks and packaged foods, so it cannot be
# treated as an edge case.
_WORD_NUMBERS = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6,
    "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "ELEVEN": 11, "TWELVE": 12,
    "EIGHTEEN": 18, "TWENTYFOUR": 24,
}

_RELATIVE_PERIOD = re.compile(
    r"(?:BEST\s*BEFORE|USE\s*(?:WITHIN|BEFORE)|EXPIRES?)\D{0,12}?"
    r"(\d{1,2}|[A-Z]{3,10})\s*"
    r"(DAY|WEEK|MONTH|YEAR)S?\b",
    re.IGNORECASE,
)


def parse_relative_period(text: str) -> tuple[int, str] | None:
    """Extract a shelf-life period such as (9, 'MONTH') from label text.

    Returns None when the text carries no relative period.
    """
    if not text:
        return None

    match = _RELATIVE_PERIOD.search(text.upper())
    if not match:
        return None

    raw_amount, unit = match.group(1), match.group(2).upper()

    if raw_amount.isdigit():
        amount = int(raw_amount)
    else:
        amount = _WORD_NUMBERS.get(re.sub(r"[^A-Z]", "", raw_amount))
        if amount is None:
            return None

    if not 1 <= amount <= 120:
        return None

    return amount, unit


def add_period(iso_date: str, amount: int, unit: str) -> str | None:
    """Add a shelf-life period to an ISO date, preserving its precision.

    A month-precision input yields a month-precision result: deriving a day
    from a pack that never printed one would be fabrication.
    """
    parts = iso_date.split("-")
    if len(parts) not in (2, 3):
        return None

    try:
        year, month = int(parts[0]), int(parts[1])
        day = int(parts[2]) if len(parts) == 3 else None
    except ValueError:
        return None

    months = {"MONTH": amount, "YEAR": amount * 12}.get(unit)
    if months is not None:
        total = (year * 12 + (month - 1)) + months
        new_year, new_month = divmod(total, 12)
        new_month += 1
        if day is None:
            return f"{new_year:04d}-{new_month:02d}"
        capped = min(day, calendar.monthrange(new_year, new_month)[1])
        return f"{new_year:04d}-{new_month:02d}-{capped:02d}"

    days = {"DAY": amount, "WEEK": amount * 7}.get(unit)
    if days is None:
        return None

    if day is None:
        # Day-level arithmetic on a month-only date would invent precision.
        return None

    try:
        from datetime import timedelta

        result = date(year, month, day) + timedelta(days=days)
    except ValueError:
        return None
    return result.isoformat()


def normalize(raw: str, value_type: str, date_precision: str = "month") -> NormalizedValue:
    """Dispatch to the normalizer for a field's declared value type."""
    if value_type == "date":
        return normalize_date(raw, date_precision)
    if value_type == "currency":
        return normalize_currency(raw)
    if value_type == "code":
        return normalize_code(raw)
    return NormalizedValue(raw.strip() or None)
