"""Field extraction from OCR tokens (CLAUDE.md sections 8 and 9).

This is not general document understanding. It is a small set of known SKU
label fields, found by matching a printed label to its value using position.

Strategy, in order of preference per field:

    1. inline    label and value in one token: "BATCH NO A23C91"
    2. same-line label token, value token to its right on the same line
    3. below     value token beneath the label, horizontally aligned

Every candidate must pass a plausibility gate for the field's value type
before it is accepted. Without that gate a label happily adopts whatever
token sits nearest, which is how "MFG" ends up holding a batch code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings
from app.services import normalizer
from app.services.ocr_service import OcrToken

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    value_type: str
    aliases: tuple[str, ...]
    date_precision: str = "month"


@dataclass
class FieldCandidate:
    """One extraction attempt, before validation and scoring."""

    field: str
    raw_value: str
    normalized: normalizer.NormalizedValue
    label_token: OcrToken | None
    value_token: OcrToken
    strategy: str
    label_match_quality: float
    spatial_quality: float

    @property
    def ocr_confidence(self) -> float:
        if self.label_token is None or self.label_token is self.value_token:
            return self.value_token.confidence
        # The label's own recognition matters: a misread label means the
        # association itself is suspect, not just the value.
        return min(self.value_token.confidence, self.label_token.confidence)


def _canonical(text: str) -> str:
    """Collapse punctuation and spacing so 'B.NO.' == 'b no' == 'BNO'."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


@lru_cache
def load_field_specs(config_path: str | None = None) -> tuple[FieldSpec, ...]:
    """Load the alias table. Aliases live in YAML, never in code (section 8)."""
    path = Path(config_path) if config_path else get_settings().field_alias_config
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    specs = []
    for name, body in (raw.get("fields") or {}).items():
        # Longest alias first so "BATCH NO" is tried before "BATCH".
        aliases = tuple(
            sorted((a for a in body.get("aliases", [])), key=len, reverse=True)
        )
        specs.append(
            FieldSpec(
                name=name,
                value_type=body.get("value_type", "code"),
                aliases=aliases,
                date_precision=body.get("date_precision", "month"),
            )
        )
    return tuple(specs)


@lru_cache
def load_config(config_path: str | None = None) -> dict:
    """Load weights and penalties from the same file as the aliases."""
    path = Path(config_path) if config_path else get_settings().field_alias_config
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


# --- plausibility gates ----------------------------------------------------

_DATE_HINT = re.compile(
    r"(\d{1,2}\s*[/\-.]\s*\d{2,4}|\d{4,6}|[A-Z]{3,9}\s*\d{2,4})", re.IGNORECASE
)

#: How far a printed pack date can plausibly sit from today.
#:
#: The parser will happily turn "2.00" into 2000-02, because it is a valid
#: calendar date. On a jar in a D-Mart aisle it is not a date at all — it is
#: the "2.00" of "RS.2.00", and treating it as one cost the MRP on a real
#: capture: the price was discarded for overlapping a date that never existed.
#:
#: The window is deliberately generous. Nothing on sale was manufactured a
#: decade ago, and almost nothing carries an expiry fifteen years out, so a
#: date outside it is a misread rather than a fact about the product.
#:
#: This lives in the extractor and NOT in the normalizer, which is the whole
#: point. The normalizer also runs on /confirm, where the operator is the final
#: authority (CLAUDE.md section 4) — refusing an odd date they typed
#: deliberately would be the wrong end of the pipeline to be strict at.
_MAX_YEARS_PAST = 10
_MAX_YEARS_FUTURE = 15


def _plausible_pack_date(value: str | None) -> bool:
    """Is this normalized date one a product on a shelf could carry?"""
    if not value:
        return False
    try:
        year = int(value[:4])
    except ValueError:
        return False
    this_year = date.today().year
    return this_year - _MAX_YEARS_PAST <= year <= this_year + _MAX_YEARS_FUTURE
_NUMBER_HINT = re.compile(r"\d")
_CODE_HINT = re.compile(r"[A-Z0-9]{3,}", re.IGNORECASE)


#: Boilerplate that sits next to the fields we want and is never a value.
#:
#: "(Inclusive of all taxes)" follows MRP on essentially every Indian pack, and
#: was being adopted as a batch code on real captures — the extractor produced
#: `batchNumber=INCLUSIVEOF` from a photograph of a medicine carton.
_BOILERPLATE = {
    "INCLUSIVEOFALLTAXES",
    "INCLOFALLTAXES",
    "INCLUSIVEOF",
    "INCLOF",
    "ALLTAXES",
    "PERSACHET",
    "NETCONTENTS",
    "NETWT",
    "NETWEIGHT",
    "MFDBY",
    "MKTBY",
    "MARKETEDBY",
    "MANUFACTUREDBY",
    "CUSTOMERCARE",
}


#: A statutory number, printed with its own label, that is not a batch code.
#:
#: Indian packs carry several of these and they sit exactly where a batch code
#: sits. Measured: a pack printing "Lic. No. 1001" in the top corner had it
#: adopted as the batch number, because the licence line and the "B.NO." label
#: happened to share a row while the real value "J100" sat on the row below.
#:
#: A token carrying one of these words is already labelled as something else,
#: so it is never an unlabelled value.
_STATUTORY_NUMBER = re.compile(
    r"\b(?:LIC|LICENCE|LICENSE|REGN|FSSAI|CIN|GSTIN|TIN|PAN|UDYAM|USP)\b",
    re.IGNORECASE,
)

#: A glyph outside Latin-1. The recogniser is multilingual and returns CJK on
#: dot-matrix print it cannot read - "L三F.F0.359" for a licence code beside a
#: batch label. normalize_code silently deletes such characters, which turns a
#: garbled read into a clean-looking wrong answer ("LFF0359"). For a code field,
#: where nothing in the value can reveal an error, a garbled read must be
#: rejected rather than tidied: section 24 rates a missing code above a wrong
#: one. Indian retail batch codes are Latin alphanumeric.
_NON_LATIN = re.compile(r"[^\x00-\xFF]")


def _is_plausible(text: str, value_type: str, all_aliases: set[str]) -> bool:
    """Reject a candidate that cannot be this field's value.

    A token that is itself a known field label is never a value — that is how
    "MFG" adjacent to "EXP" produces two fields both holding the word "EXP".
    """
    stripped = _canonical(text)
    if not stripped:
        return False
    if stripped in all_aliases:
        return False
    if stripped in _BOILERPLATE:
        return False

    if value_type == "date":
        return bool(_DATE_HINT.search(text))
    if value_type == "currency":
        return _looks_like_price(text)
    if value_type == "code":
        if not _CODE_HINT.search(text):
            return False
        if _STATUTORY_NUMBER.search(text):
            return False
        if _NON_LATIN.search(text):
            return False
        # A batch or lot code carries at least one digit. Purely alphabetic
        # runs next to a "BATCH" label are prose — statutory text, an address,
        # a marketing line — not the code. Verified against real captures:
        # this is what separates "GSB0134" from "INCLUSIVEOF".
        if not any(char.isdigit() for char in text):
            return False
        return True
    return True


# --- spatial association ---------------------------------------------------


def _centre_y(token: OcrToken) -> float:
    return token.y + token.height / 2


def _same_line(label: OcrToken, candidate: OcrToken) -> bool:
    """True when a label and a candidate sit on the same printed line.

    Compares line CENTRES rather than box overlap. A pack is photographed at
    whatever angle the operator is holding it, so a printed row arrives with
    the label and its value on a slight diagonal, and the two boxes may barely
    overlap while plainly belonging to the same line.

    Measured on a real bottle capture, "Batch No.:" and its value overlapped by
    35px against a 50px requirement, and "Expiry Date:" missed by a single
    pixel. Both were rejected, and both are obviously the same row to a reader.

    The tolerance scales with the taller box, so it holds at any capture
    distance.
    """
    tallest = max(label.height, candidate.height) or 1
    return abs(_centre_y(label) - _centre_y(candidate)) < 0.8 * tallest


def _horizontally_aligned(label: OcrToken, candidate: OcrToken) -> bool:
    """Value sits under the label, roughly sharing a left edge or centre."""
    if abs(candidate.x - label.x) < max(label.width, 40) * 0.6:
        return True
    label_cx, _ = label.center
    cand_cx, _ = candidate.center
    return abs(cand_cx - label_cx) < max(label.width, 40) * 0.8


def _spatial_score(label: OcrToken, candidate: OcrToken, strategy: str) -> float:
    """Closer is better; same-line beats below.

    Distance is measured in label-heights so the score does not change when
    the same label is photographed closer or further away.
    """
    unit = max(label.height, 1)

    if strategy == "same_line":
        gap = max(0, candidate.x - label.right) / unit
        # The divisor is deliberately forgiving. SKU labels commonly print
        # label and value as aligned columns, so a wide horizontal gap is
        # normal layout rather than evidence of a bad association — sharing a
        # line is itself the strong signal. Penalising distance hard put every
        # correctly-extracted field into REVIEW, which makes the band useless.
        horizontal = max(0.0, 1.0 - gap / 30.0)

        # Vertical alignment has to be scored too, and for the same reason
        # that the horizontal term is forgiving: in a two-column label the
        # values all share an x, so horizontal distance cannot tell one row
        # from the next. Measured on a real capture, "Mfg. Date:" scored 0.958
        # against its own value and 0.959 against the row below it, and the
        # wrong row won by ten pixels of noise — putting the expiry date into
        # the manufacturing field.
        #
        # Scaled by the shorter box, because a label box that swallowed two
        # printed lines is exactly the case that needs discriminating.
        pitch = max(min(label.height, candidate.height), 1)
        offset = abs(_centre_y(candidate) - _centre_y(label)) / pitch
        vertical = max(0.0, 1.0 - offset / 2.0)

        return horizontal * vertical

    gap = max(0, candidate.y - label.bottom) / unit
    # Below-label association is inherently weaker than same-line, so it is
    # capped lower even at zero distance.
    return max(0.0, 0.85 - gap / 10.0)


#: A candidate scoring this or less has no spatial evidence behind it.
#:
#: The score already falls to zero once a candidate is absurdly far from its
#: label, but zero was still being accepted whenever nothing else competed —
#: and on a pack whose values are unreadable, nothing else does. That is how
#: "M.R.P.RS:" adopted the toll-free number printed 121px below it, and
#: "Batch No.:" adopted the same number from 322px away.
#:
#: Being the only candidate is not evidence. Returning nothing is the correct
#: answer when the value beside the label could not be read.
_MIN_SPATIAL_SCORE = 0.0

#: What a retail pack price can be, in rupees.
#:
#: Shared by both the labelled and unlabelled paths. Only the unlabelled one
#: used to check it, so a labelled MRP had no magnitude gate at all and
#: "M.R.P.RS:" beside a phone number produced an MRP of 8,004,420,168.00.
_MIN_PRICE = 1.0
_MAX_PRICE = 100000.0


def _plausible_price(value: str) -> bool:
    if not _numeric(value):
        return False
    return _MIN_PRICE <= float(value.replace(",", "")) <= _MAX_PRICE


def _find_label_matches(
    tokens: list[OcrToken], spec: FieldSpec
) -> list[tuple[int, str, float, str]]:
    """Locate tokens containing one of this field's aliases.

    Returns (token index, alias, match quality, remainder-after-label).
    """
    matches = []
    for index, token in enumerate(tokens):
        canonical = _canonical(token.text)
        if not canonical:
            continue

        for alias in spec.aliases:
            alias_key = _canonical(alias)
            if not alias_key:
                continue

            if canonical == alias_key:
                matches.append((index, alias, 1.0, ""))
                break

            if canonical.startswith(alias_key):
                remainder = token.text
                # Recover the printed remainder by trimming the same number of
                # significant characters the alias consumed.
                consumed = 0
                cut = 0
                for position, char in enumerate(token.text):
                    if re.match(r"[A-Za-z0-9]", char):
                        consumed += 1
                    if consumed == len(alias_key):
                        cut = position + 1
                        break
                remainder = token.text[cut:].strip(" .:-")
                # An exact prefix is a strong signal; a long trailing remainder
                # means the token is probably a sentence, not a label.
                quality = 0.9 if remainder else 1.0
                matches.append((index, alias, quality, remainder))
                break

    return matches


def _looks_rotated(tokens: list[OcrToken]) -> bool:
    """True when the text runs vertically down the image.

    Operators photograph packs in whatever orientation the pack is held, and a
    carton side or a can base is very often captured with the print running
    bottom-to-top. PaddleOCR reads rotated text correctly, but it returns an
    axis-aligned box around it, so a horizontal line of rotated text has a TALL
    narrow box. Every spatial rule here is written in reading space, so without
    this the label and its value never share a "line".

    Judged on tokens of four characters or more: a short token like "MFG" is
    nearly square either way and carries no signal.
    """
    meaningful = [t for t in tokens if len(t.text) >= 4 and t.width > 0 and t.height > 0]
    if len(meaningful) < 2:
        return False

    taller = sum(1 for t in meaningful if t.height > t.width * 1.5)
    return taller > len(meaningful) / 2


def _to_reading_space(tokens: list[OcrToken]) -> list[OcrToken]:
    """Rotate token geometry so vertical text reads left-to-right.

    Only the coordinates move. The recognized text is already correct, so this
    is a cheap transform rather than a second OCR pass: (x, y) -> (y, -x), with
    width and height swapped.
    """
    if not tokens:
        return tokens

    max_bottom = max(t.bottom for t in tokens)
    rotated = [
        OcrToken(
            text=t.text,
            x=t.y,
            y=max_bottom - t.right,
            width=t.height,
            height=t.width,
            confidence=t.confidence,
            variant=t.variant,
        )
        for t in tokens
    ]
    rotated.sort(key=lambda t: (round(t.y / 10), t.x))
    return rotated


def extract_fields(tokens: list[OcrToken]) -> dict[str, FieldCandidate]:
    """Extract the best candidate per field from a token list."""
    if _looks_rotated(tokens):
        logger.info("Tokens look rotated; mapping into reading space")
        tokens = _to_reading_space(tokens)

    specs = load_field_specs()
    all_aliases = {
        _canonical(alias) for spec in specs for alias in spec.aliases
    }

    results: dict[str, FieldCandidate] = {}

    for spec in specs:
        candidates: list[FieldCandidate] = []

        for label_index, alias, quality, remainder in _find_label_matches(tokens, spec):
            label_token = tokens[label_index]

            # 1. inline — value printed in the same token as the label
            if remainder and _is_plausible(remainder, spec.value_type, all_aliases):
                normalized = normalizer.normalize(
                    remainder, spec.value_type, spec.date_precision
                )
                if normalized.ok and (
                    spec.value_type != "date"
                    or _plausible_pack_date(normalized.value)
                ):
                    candidates.append(
                        FieldCandidate(
                            field=spec.name,
                            raw_value=remainder,
                            normalized=normalized,
                            label_token=label_token,
                            value_token=label_token,
                            strategy="inline",
                            label_match_quality=quality,
                            spatial_quality=1.0,
                        )
                    )
                    continue

            # 2/3. neighbouring token, same line first then below
            #
            # A third "next row and to the right" strategy was tried, for packs
            # whose value column is offset downward as well as across. Measured:
            # it won one code field and turned two core misses into two core
            # wrongs, which section 24 rates as the worse outcome. Reverted.
            for strategy in ("same_line", "below"):
                best = _best_neighbour(
                    tokens, label_index, spec, strategy, all_aliases
                )
                if best is None:
                    continue

                value_token, normalized = best
                candidates.append(
                    FieldCandidate(
                        field=spec.name,
                        raw_value=value_token.text,
                        normalized=normalized,
                        label_token=label_token,
                        value_token=value_token,
                        strategy=strategy,
                        label_match_quality=quality,
                        spatial_quality=_spatial_score(
                            label_token, value_token, strategy
                        ),
                    )
                )
                break

        if candidates:
            # Prefer the association we are most sure about, then the value the
            # OCR read most confidently.
            candidates.sort(
                key=lambda c: (
                    c.label_match_quality * c.spatial_quality,
                    c.ocr_confidence,
                ),
                reverse=True,
            )
            results[spec.name] = candidates[0]

    _split_shared_date(tokens, results)
    _derive_relative_expiry(tokens, results)
    _infer_unlabelled(tokens, results)
    return results


def _split_shared_date(
    tokens: list[OcrToken], results: dict[str, FieldCandidate]
) -> None:
    """Separate a manufacturing and expiry date that landed on one token.

    Nearest-neighbour association breaks when a capture is slightly skewed. The
    left-hand label column drifts down relative to the right-hand value column,
    so by the time the reader reaches "Use By" the vertical offset to its own
    value exceeds the offset to the row above, and BOTH date labels resolve to
    the same token while the real second date sits unclaimed:

        Mfd.:    (143,199)      13/11/25  (306,229)   <- both labels
        Use By:  (144,263)      12/08/26  (327,285)      chose this one

    Only fires when the two fields point at the same token AND exactly two
    plausible dates were printed, which is the case the pack itself resolves:
    section 11 requires manufacture before expiry, so the earlier date is the
    manufacturing date. That is the same convention _infer_unlabelled already
    applies to a bare stamp, applied here to a labelled pack whose geometry
    misled the reader.

    Deliberately not a general exclusivity rule. Forbidding two fields from
    sharing any token was tried and reverted: it starved fields greedily and
    cost two core points.
    """
    mfg = results.get("manufacturingDate")
    exp = results.get("expiryDate")
    if mfg is None or exp is None or mfg.value_token is not exp.value_token:
        return

    seen: set[str] = set()
    printed: list[tuple[str, OcrToken]] = []
    for token in tokens:
        text = _split_run_together_dates(token.text)

        # The whole token as well as the date-shaped spans inside it. _DATE_TOKEN
        # insists on a separator, and the packs this rule exists for are exactly
        # the ones whose separators were recognised as digits: "16/06/2026" comes
        # back as "1610612026", which the normalizer repairs and this regex never
        # sees. Both sources are deduplicated by normalized value.
        raws = [m.group(1) for m in _DATE_TOKEN.finditer(text)] + [text]

        for raw in raws:
            # A stamped time is not a date. Inkjet coders print "13:02:38"
            # beside the date they stamp, and read as DD/MM/YY that is a valid
            # 2038 date well inside the plausible window - a third date, which
            # silently disables this rule on the packs that most need it.
            if ":" in raw:
                continue
            normalized = normalizer.normalize_date(raw, "day")
            value = normalized.value
            if not (normalized.ok and _plausible_pack_date(value)):
                continue
            if value in seen:
                continue
            seen.add(value)
            printed.append((value, token))

    # Three or more dates is a pack printing something this rule cannot
    # arbitrate — a packed date, a best-before and a batch date, say — and
    # guessing between them would be exactly the fabrication section 11 forbids.
    if len(printed) != 2:
        return

    printed.sort(key=lambda p: p[0])
    (early_value, early_token), (late_value, late_token) = printed

    results["manufacturingDate"] = replace(
        mfg,
        raw_value=early_token.text,
        value_token=early_token,
        normalized=normalizer.NormalizedValue(
            early_value, is_ambiguous=True,
            notes=["both date labels resolved to one token; took the earlier "
                   "of the two printed dates as manufacture"],
        ),
    )
    results["expiryDate"] = replace(
        exp,
        raw_value=late_token.text,
        value_token=late_token,
        normalized=normalizer.NormalizedValue(
            late_value, is_ambiguous=True,
            notes=["both date labels resolved to one token; took the later "
                   "of the two printed dates as expiry"],
        ),
    )


# A bare inkjet stamp: a price, sometimes a per-unit price, two dates and a
# code, printed with no field names at all. Roughly a third of captured packs
# look like this, so the three fields that are always present — MRP,
# manufacturing date and expiry date — have to be recoverable without a label.
_DATE_TOKEN = re.compile(
    r"\b(\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2,4}"
    r"|\d{1,2}\s*[/.\-]\s*\d{2,4}"
    r"|\d{1,2}\s*[A-Z]{3,9}\s*\d{2,4})\b",
    re.IGNORECASE,
)

#: A price carries a currency marker or a trailing /-; a bare number does not
#: qualify, or every quantity on the pack would look like a price.
#
# Named groups, because the branches are not equally trustworthy and the code
# below treats them differently. Numbered groups shifted silently when a branch
# was added.
_PRICE_TOKEN = re.compile(
    r"(?:RS\.?|INR|₹|MRP)\s*:?\s*(?P<marked>[0-9][0-9,]*(?:\.\d{1,2})?)"
    r"|(?P<slashed>[0-9][0-9,]*(?:\.\d{1,2})?)\s*/-"
    # The same "/-" with its slash recognised as a 1 or a bar, which is the
    # identical confusion the date repair in the normalizer exists for: on a
    # dot-matrix stamp "190/-" comes back as "1901-". Treated as weak evidence
    # rather than as a marked price, because unlike a real "/-" the marker is
    # inferred, and "2001-2002" in an address would otherwise read as 200.
    r"|(?P<misread>[0-9][0-9,]*(?:\.\d{1,2})?)\s*[1|]-"
    # A bare decimal, for stamps that print no currency marker at all. Kept
    # last and still filtered by the per-unit strip and the plausible-price
    # range, or every weight on the pack would look like a price.
    r"|\b(?P<bare>[0-9]{1,5}\.[0-9]{2})\b",
    re.IGNORECASE,
)

#: Per-unit price. Never the MRP. Shared with the normalizer so the two agree.
_PER_UNIT = normalizer.PER_UNIT_PRICE

#: "USP" is Unit Sale Price — the per-unit figure's own label, printed on most
#: Indian FMCG packs right beside it. Anything it introduces is by definition
#: not the MRP.
_UNIT_PRICE_LABEL = re.compile(r"\bUSP\b", re.IGNORECASE)

#: Currency markers, which are the only letters allowed to sit inside a price.
_CURRENCY_MARKER = re.compile(r"RS|INR|MRP|M\.?R\.?P|₹|/-|/=", re.IGNORECASE)

#: The same markers, but as evidence rather than as something to strip.
#:
#: Requiring a word boundary is the whole difference. Stripping "RS" out of a
#: value is harmless when it over-matches; accepting a bare decimal as an MRP
#: because the pack contains the letters R and S is not.
_MONEY_MENTION = re.compile(
    r"\bRS\b|\bINR\b|\bMRP\b|\bM\.?R\.?P\.?|₹|\d\s*/-|\d\s*/=", re.IGNORECASE
)

#: A bracketed aside, in either ASCII or fullwidth brackets. Beside a price it
#: is the tax disclaimer or the per-unit rate, never the amount itself.
_BRACKETED = re.compile(r"[(（][^)）]*[)）]?")

#: A complete day-month-year date. Two separators, which no price has.
_FULL_DATE = re.compile(r"\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*\d{2,4}")


def _looks_like_price(text: str) -> bool:
    """True when a token could be a retail price rather than merely numeric.

    The labelled path used to accept any token containing a digit, which is
    far too weak: beside an "MRP:" label the extractor accepted the garbage
    token "6MIS" as 6.00, and the printed date "MAY-26." as 26.00, on real
    captures. Both are confident wrong values for a core field.

    A price is a number, optionally wrapped in currency markers and
    punctuation. Once the markers and any per-unit price are removed, no
    letters may remain — that is what separates "Rs.60.00" and "250.00(0.31/9)"
    from "MAY-26." and "6MIS", without demanding a decimal point that a pack
    printing "MRP 245" does not have.
    """
    if _UNIT_PRICE_LABEL.search(text):
        return False

    # A printed calendar date is never a price, and it is the most expensive
    # thing that ever looked like one. On SCAN-000196 the token "27/06/2026"
    # sat one row below the "MRP (incl. of all taxes)" label and the extractor
    # took it, read the year as the amount, and reported an MRP of 2026.00 for
    # a pack marked 85.00 - a confidently wrong core field, which section 24
    # ranks as the worst outcome this project has.
    #
    # Keyed to the THREE-part shape rather than to "parses as a date", which
    # would be far too greedy: "12.25" is a valid price and also a valid
    # month/year. Two separators is what no price has.
    if _FULL_DATE.search(text):
        return False

    # A parenthetical beside a price is never the price. Packs print
    # "MRP (Incl. of all taxes) : 23.00 (₹:0.23/g)", and both brackets have to
    # go before the letter test below — otherwise the statutory phrase inside
    # the first one disqualifies a price that is plainly there. Fullwidth
    # brackets included: the recogniser emits them on metallic print.
    remainder = _BRACKETED.sub(" ", text)
    remainder = _PER_UNIT.sub(" ", remainder)
    remainder = _CURRENCY_MARKER.sub(" ", remainder)
    if not _NUMBER_HINT.search(remainder):
        return False

    # Only ASCII letters disqualify. The multilingual recogniser prefixes CJK
    # glyphs to Latin text on dot-matrix and metallic print — it returned
    # "年319.00（0.639）" for a jar marked 319.00 — and treating those as
    # letters threw away the price along with the noise.
    return not any("A" <= char <= "Z" or "a" <= char <= "z" for char in remainder)


def _infer_unlabelled(
    tokens: list[OcrToken], results: dict[str, FieldCandidate]
) -> None:
    """Recover MRP and the two dates from a stamp that carries no field names.

    Only fills fields the labelled pass could not find, so a pack that does
    print "Batch No." is never overridden by a guess.

    Two conventions do the work, both near-universal on Indian FMCG stamps:

    * Of two dates, the earlier is manufacture and the later is expiry. A pack
      does not expire before it is made, so ordering is safe where labels are
      not.
    * A price carries a currency marker or a trailing "/-", and a per-unit
      price is excluded first. Without that, "0.25/g" outbids the real MRP.

    Everything inferred here is marked so confidence reflects that it was
    positional rather than read from a label.
    """
    wanted = {"mrp", "manufacturingDate", "expiryDate"}
    if wanted.issubset(results.keys()):
        return

    dates: list[tuple[str, str, OcrToken]] = []
    prices: list[tuple[str, OcrToken]] = []

    # Does this pack talk about money at all?
    #
    # A bare decimal is the weakest evidence in the extractor — it is any
    # number with two decimal places — and it exists only for stamps that print
    # no currency marker. On a pack that names no price anywhere it is far more
    # likely to be something else: "92.10" beside "USE BEFORE" on a real
    # capture is a mangled date, and the pack carries BATCH NO., USE BEFORE,
    # STORAGE CONDITION and NET VOL. but no price of any kind. Reading it as an
    # MRP is a false accept, which section 24 rates as the most serious failure
    # class this project has.
    #
    # So a bare decimal needs corroboration: some token, anywhere on the label,
    # that mentions money. A marked price ("RS.2.00", "245/-") says what it is
    # and needs none.
    # Not _CURRENCY_MARKER: that one is built for STRIPPING a marker out of a
    # value and matches "RS" anywhere, which "RSIY" — a mangled token on the
    # very pack this gate exists for — satisfies. Corroboration has to be
    # stricter than removal, so the marker must stand as a word.
    mentions_money = any(_MONEY_MENTION.search(token.text) for token in tokens)

    for token in tokens:
        text = _split_run_together_dates(token.text)
        if _canonical(text) in _BOILERPLATE:
            continue

        without_unit = _PER_UNIT.sub(" ", text)

        # Dates are normalized and checked for plausibility HERE, not later,
        # because the spans they occupy are what suppress a bare decimal from
        # being read as a price. A span that turns out not to be a date must
        # not suppress anything: "RS.2.00" came back as "F3.2.00", whose
        # "2.00" parses as the year 2000, and the real MRP was discarded for
        # colliding with a date that does not exist.
        date_spans = []
        for match in _DATE_TOKEN.finditer(without_unit):
            raw = match.group(1)
            normalized = normalizer.normalize_date(raw, "day")
            if not (normalized.ok and _plausible_pack_date(normalized.value)):
                continue
            dates.append((normalized.value, raw, token))
            date_spans.append(match.span())

        # And the whole token, for a date whose separator was recognised as a
        # digit. _DATE_TOKEN requires a separator to have survived in the
        # printed text, so on "27/0672026" it finds nothing and the pack looks
        # like it carries ONE date - which made the manufacturing date take the
        # Use By value and left the mangled token free to be adopted as a batch
        # code. Times are excluded: "13:02:38" reads as a valid 2038 date.
        if not date_spans and ":" not in without_unit:
            whole = normalizer.normalize_date(without_unit, "day")
            if whole.ok and _plausible_pack_date(whole.value):
                dates.append((whole.value, without_unit, token))
                date_spans.append((0, len(without_unit)))

        for match in _PRICE_TOKEN.finditer(without_unit):
            value = (
                match.group("marked")
                or match.group("slashed")
                or match.group("misread")
                or match.group("bare")
            )
            # A retail pack price sits in a sane range. Outside it the number
            # is a weight, a licence number or a phone number.
            if not value or not _plausible_price(value):
                continue
            # A date is not a price. "2.06.2028" contains "2.06", which the
            # bare-decimal branch reads as two rupees six paise.
            #
            # Only the two weak branches are checked. A match carrying a
            # currency marker or a genuine trailing "/-" has said what it is,
            # and "M.R.P. Rs. 20.00" must not be discarded because "20.00" also
            # looks like a day-month pair. The inferred "/-" gets no such
            # benefit: its marker is a guess about one character.
            if match.group("bare") or match.group("misread"):
                if not mentions_money:
                    continue
                start, end = match.span()
                if any(start < d_end and d_start < end for d_start, d_end in date_spans):
                    continue
            prices.append((value, token))

    if "mrp" not in results and prices:
        # Largest wins. A stamp that shows both a pack price and a smaller
        # figure is showing a unit or promotional price alongside the MRP.
        raw, token = max(
            prices,
            key=lambda p: float(p[0].replace(",", "")) if _numeric(p[0]) else -1,
        )
        normalized = normalizer.normalize_currency(raw)
        if normalized.ok:
            results["mrp"] = _inferred("mrp", raw, normalized, token)

    # Already normalized and plausibility-checked above, where the spans were
    # needed for the price exclusion.
    # Distinct dates only: the same stamp read twice is not two dates.
    seen: set[str] = set()
    unique = [p for p in dates if not (p[0] in seen or seen.add(p[0]))]
    unique.sort(key=lambda p: p[0])

    if len(unique) >= 2:
        earliest, latest = unique[0], unique[-1]
        if "manufacturingDate" not in results:
            results["manufacturingDate"] = _inferred(
                "manufacturingDate", earliest[1],
                normalizer.NormalizedValue(earliest[0], is_ambiguous=True,
                                           notes=["inferred: earlier of two printed dates"]),
                earliest[2],
            )
        if "expiryDate" not in results:
            results["expiryDate"] = _inferred(
                "expiryDate", latest[1],
                normalizer.NormalizedValue(latest[0], is_ambiguous=True,
                                           notes=["inferred: later of two printed dates"]),
                latest[2],
            )

    elif len(unique) == 1 and "manufacturingDate" not in results:
        # One date on the stamp is the manufacturing date.
        #
        # This is the printing convention, not a deduction: a bare stamp
        # carries batch and manufacture, or batch, manufacture and expiry, and
        # never an expiry on its own. An earlier version decided this by
        # whether the date was in the past, which got the right answer on the
        # capture in front of it for the wrong reason and would have called a
        # long-dated pack's manufacture date an expiry.
        #
        # Still marked ambiguous. The value was read, not invented, but which
        # field it belongs to is positional, so it belongs in operator review.
        only = unique[0]
        results["manufacturingDate"] = _inferred(
            "manufacturingDate", only[1],
            normalizer.NormalizedValue(
                only[0], is_ambiguous=True,
                notes=["inferred: the only date on an unlabelled stamp, which "
                       "by convention is the manufacturing date"],
            ),
            only[2],
        )

    # Unconditionally, and after both date branches. This used to sit behind an
    # early return in the two-date branch, so the packs that stamp a batch code
    # beside BOTH dates - the most common stamp layout there is - were the exact
    # ones whose batch was never inferred.
    _infer_stamp_batch(tokens, results, unique)


#: A code that could be a batch number on a bare stamp.
#:
#: Letters and digits together, or a digit run of moderate length. A pure word
#: is prose and a very long digit run is a phone number, a licence or a GTIN.
_STAMP_CODE = re.compile(r"^(?=.*[0-9])[A-Z0-9][A-Z0-9\-/]{3,15}$", re.IGNORECASE)

#: A measured quantity, which is code-shaped and never a code. "100g" passes
#: the pattern above - four characters, contains a digit - and was adopted as
#: the batch number on a pack that prints none. Net weight sits near the stamp
#: on most packs, so this is not a rare collision.
_QUANTITY = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:G|GM|GMS|GRAMS?|KG|MG|ML|LTR|L|N|PCS?|NOS?)$",
    re.IGNORECASE,
)

#: How far from the date, in multiples of the date token's own height, a batch
#: code can sit and still belong to the same stamp.
_STAMP_PROXIMITY = 2.5


def _horizontal_gap(left: OcrToken, right: OcrToken) -> float:
    """Pixels between two boxes horizontally; zero when they overlap."""
    return max(0, max(left.x, right.x) - min(left.right, right.right))


def _infer_stamp_batch(
    tokens: list[OcrToken],
    results: dict[str, FieldCandidate],
    dated: list[tuple[str, str, OcrToken]],
) -> None:
    """Recover the batch code from a stamp that carries no field names.

    A bare stamp always prints a batch number alongside the manufacturing
    date — that is the printing convention, and it is why a jar stamped
    "MN2605121 05/26" should yield both rather than only the date.

    The date is what makes this safe to attempt. Rather than scanning the whole
    label for anything code-shaped, which would adopt a licence number or a
    postcode on any pack that prints one, the search is anchored to the token
    the date came from: the same token first, then its immediate neighbours.
    Off a stamp there is nothing to anchor to and nothing is inferred.
    """
    if "batchNumber" in results or not dated:
        return

    # A value already claimed by another field is not also the batch number.
    #
    # On SCAN-000196 the pack printed a Lot No. and no batch at all. The lot was
    # extracted correctly from its own label, and then this inference adopted
    # the very same token as the batch, because a code sitting beside a date is
    # exactly what it looks for. The result was a batch number on a pack that
    # has none - a false accept, which section 24 ranks worst, and one the
    # operator cannot catch by reading the value because the value is real.
    #
    # Deliberately narrow: this only stops the UNLABELLED inference from
    # duplicating a value a LABEL already explained. It is not the general
    # cross-field exclusivity that was tried and reverted for starving fields.
    # Tokens a printed LABEL already accounted for.
    #
    # By token rather than by value: once a dropped prefix is restored the lot
    # reads "M2 ZX2626PZAB" while the bare token still says "ZX2626PZAB", so
    # comparing strings sees two different codes and lets the duplicate
    # through. The token is the thing that can only belong to one field.
    #
    # And by LABELLED strategies only. The dates on a bare stamp are inferred
    # from this same token block, so treating every claim as exclusive would
    # block the batch search on exactly the packs it exists for - which is what
    # a first attempt at this did.
    claimed = {
        id(candidate.value_token)
        for candidate in results.values()
        if candidate.normalized.ok
        and candidate.strategy in ("inline", "same_line", "below")
    }

    _, raw_date, date_token = dated[0]
    all_aliases = {
        _canonical(alias) for spec in load_field_specs() for alias in spec.aliases
    }

    def usable(text: str) -> str | None:
        # "#" and "*" are footnote markers, not part of the code. A pack that
        # prints "See below for #Ref. no. & *Mfd in" ties its label to its value
        # with them, so the value arrives as "#RC-DS5720" and failed the very
        # first character of _STAMP_CODE.
        candidate = text.strip(" .,:;-#*†‡")
        if not _STAMP_CODE.match(candidate):
            return None
        if _canonical(candidate) in _BOILERPLATE or _canonical(candidate) in all_aliases:
            return None
        # A date, a time or a price is not a batch code.
        #
        # Checked by NORMALIZING rather than by pattern. "27/0672026" is a date
        # with one separator misread, which _DATE_TOKEN does not match, and it
        # was adopted as a batch number on a pack that prints none.
        if _DATE_TOKEN.search(candidate) or ":" in candidate:
            return None
        as_date = normalizer.normalize_date(candidate, "day")
        if as_date.ok and _plausible_pack_date(as_date.value):
            return None
        if _QUANTITY.match(candidate):
            return None
        if _MONEY_MENTION.search(candidate):
            return None
        normalized = normalizer.normalize(candidate, "code")
        return normalized.value if normalized.ok else None

    # The stamp usually prints both in one run: "MN2605121 05/26".
    # No guard on the date token itself. A label having explained the DATE says
    # nothing about the code printed beside it: on a pack reading
    # "#RC-DS5720 / *07.2025" the date is labelled and the batch is not, and an
    # early return here cost that batch entirely. Only the neighbour search is
    # filtered, which is where a duplicate could actually come from.
    remainder = date_token.text.replace(raw_date, " ")
    for part in remainder.split():
        value = usable(part)
        if value:
            results["batchNumber"] = _inferred(
                "batchNumber", part,
                normalizer.NormalizedValue(
                    value, is_ambiguous=True,
                    notes=["inferred: printed with the date on an unlabelled stamp"],
                ),
                date_token,
            )
            return

    # Otherwise the line above or below it, within the same stamp block.
    reach = max(date_token.height, 1) * _STAMP_PROXIMITY
    neighbours = sorted(
        (
            token for token in tokens
            if token is not date_token
            and id(token) not in claimed
            and abs(_centre_y(token) - _centre_y(date_token)) <= reach
            # Near horizontally as well as vertically. "Printed with the date"
            # means beside it, not merely somewhere on the same row of a label
            # that spans the whole pack: on one capture the address fragment
            # "than-301019" sat at x=0 while the stamp was at x=482, shared a
            # row, and was adopted as the batch number for a pack that prints
            # none.
            and _horizontal_gap(token, date_token) <= reach
        ),
        key=lambda token: abs(_centre_y(token) - _centre_y(date_token)),
    )

    for token in neighbours:
        for part in token.text.split():
            value = usable(part)
            if value:
                results["batchNumber"] = _inferred(
                    "batchNumber", part,
                    normalizer.NormalizedValue(
                        value, is_ambiguous=True,
                        notes=["inferred: printed beside the date on an "
                               "unlabelled stamp"],
                    ),
                    token,
                )
                return


#: Two dates printed with no gap between them. Inkjet stamps run fields
#: together often enough that OCR returns "22/06/2621/03/27" as one token, and
#: a greedy date match then reads the year as 2621.
_RUN_TOGETHER = re.compile(
    r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2})(?=\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2})"
)


def _split_run_together_dates(text: str) -> str:
    """Insert a separator between two dates printed with no gap."""
    return _RUN_TOGETHER.sub(r"\1 ", text)


def _numeric(value: str) -> bool:
    try:
        float(value.replace(",", ""))
        return True
    except ValueError:
        return False


def _inferred(
    field: str,
    raw: str,
    normalized: normalizer.NormalizedValue,
    token: OcrToken,
) -> FieldCandidate:
    """A candidate found by position rather than by label.

    Scored lower on purpose. It is a convention applied to an unlabelled
    stamp, not a value read next to its own field name, and the operator
    should be told the difference.
    """
    if not normalized.is_ambiguous:
        normalized = normalizer.NormalizedValue(
            normalized.value, is_ambiguous=True,
            notes=normalized.notes + ["inferred from an unlabelled stamp"],
        )
    return FieldCandidate(
        field=field,
        raw_value=raw,
        normalized=normalized,
        label_token=None,
        value_token=token,
        strategy="inferred",
        label_match_quality=0.5,
        spatial_quality=0.5,
    )


def _derive_relative_expiry(
    tokens: list[OcrToken], results: dict[str, FieldCandidate]
) -> None:
    """Derive an expiry date from a printed shelf life.

    Indian FMCG commonly prints "BEST BEFORE 9 MONTHS FROM PACKAGING" instead
    of an expiry date. There is nothing on the pack to read, so the value must
    be computed from the manufacturing date.

    This is not fabrication under section 11: the pack states the rule and the
    manufacturing date, and we apply the stated rule. It is nonetheless marked
    ``DERIVED_RULE`` rather than ``OCR_RULES``, and flagged ambiguous so it
    always lands in the operator's review band rather than being accepted
    silently.
    """
    if "expiryDate" in results:
        return

    manufacturing = results.get("manufacturingDate")
    if manufacturing is None or not manufacturing.normalized.ok:
        return

    for token in tokens:
        period = normalizer.parse_relative_period(token.text)
        if period is None:
            continue

        amount, unit = period
        derived = normalizer.add_period(manufacturing.normalized.value, amount, unit)
        if derived is None:
            continue

        results["expiryDate"] = FieldCandidate(
            field="expiryDate",
            raw_value=token.text,
            normalized=normalizer.NormalizedValue(
                derived,
                is_ambiguous=True,
                notes=[
                    f"derived from manufacturing date plus {amount} "
                    f"{unit.lower()}(s) stated on the pack"
                ],
            ),
            label_token=token,
            value_token=token,
            strategy="derived",
            label_match_quality=1.0,
            # Derivation is a weaker basis than reading a printed date, and the
            # score should say so.
            spatial_quality=0.6,
        )
        logger.info(
            "Derived expiry date from shelf life",
            extra={
                "manufacturingDate": manufacturing.normalized.value,
                "period": f"{amount} {unit}",
                "derived": derived,
            },
        )
        return


#: Longest prefix, in characters, that can be recovered this way. A genuine
#: dropped prefix on a printed code is a syllable, not a word.
_MAX_DROPPED_PREFIX = 3

#: How far left of a candidate the dropped prefix can sit, in candidate heights.
#: Two halves of one printed code are separated by a space, not a column gap.
_PREFIX_REACH = 1.5


def _with_dropped_prefix(
    tokens: list[OcrToken],
    label: OcrToken,
    candidate: OcrToken,
    text: str,
) -> str:
    """Put back a short leading fragment the code gate threw away.

    A code printed with a space arrives as two tokens, and when the first is
    short enough the plausibility gate rejects it for having fewer than three
    alphanumerics. The extractor then skips past it to the longer half and
    reports a truncated code that looks entirely reasonable.

    Measured on SCAN-000196: the pack prints "Lot No.: M2 ZX2626PZAB", the
    recogniser returned "M2" and "ZX2626PZAB" as separate tokens at full
    confidence, and the lot was reported as "ZX2626PZAB". The vision-language
    model read the whole thing and was marked as disagreeing with a value that
    was not wrong so much as half-present.

    Only a fragment that sits between the label and the candidate on the same
    line is eligible, so this cannot reach across a column or pick up the tail
    of a neighbouring field.
    """
    # Starts to the left, rather than ENDS before the candidate starts, for the
    # same overlap reason as above: "M2" runs to x=523 and the code beside it
    # starts at 506.
    preceding = [
        token for token in tokens
        if token is not candidate
        and token.x < candidate.x
        and token.x >= label.right
        and _same_line(candidate, token)
        and len(_canonical(token.text)) <= _MAX_DROPPED_PREFIX
        and _canonical(token.text)
    ]
    if not preceding:
        return text

    nearest = max(preceding, key=lambda token: token.x)
    if candidate.x - nearest.right > max(candidate.height, 1) * _PREFIX_REACH:
        return text
    return f"{nearest.text} {text}"


def _unit_price_split_across_tokens(
    tokens: list[OcrToken], candidate: OcrToken
) -> bool:
    """Is this number the price of a gram rather than of the pack?

    The per-unit guard works on one token, because the recogniser usually
    returns "0.30/g" or "72.12 per" whole. When it splits the number from its
    unit the guard cannot see the unit at all, and the unit price becomes the
    most price-shaped thing on the label.

    Measured on two captures of the SAME pack: the first returned "72.12 per"
    as one token and the guard held; the second returned "2.12" and "per g"
    separately - having also lost the leading 7 - and the extractor reported an
    MRP of 2.12 for a pack marked 85.00.

    Judged by joining the candidate to its right-hand neighbour and applying
    the existing per-unit rule, so there is one definition of what a unit price
    looks like rather than two that can drift apart.
    """
    # Starts to the right, rather than starts after the candidate ENDS.
    # Detection boxes on dense print overlap by a few pixels - measured, "2.12"
    # runs to x=602 and the "per g" beside it starts at 587 - so demanding a
    # clean gap found no neighbour at all and the guard never fired.
    following = [
        token for token in tokens
        if token is not candidate
        and token.x > candidate.x
        and _same_line(candidate, token)
    ]
    # Every near neighbour, not just the closest one. The closest is not
    # reliably the unit: a taller box on the row below widens the same-line
    # tolerance enough to be included, and on this pack the date "27/0672026"
    # sorted ahead of the "per g" that actually completes the unit price.
    reach = max(candidate.height, 1) * _PREFIX_REACH
    for token in sorted(following, key=lambda t: t.x):
        if token.x - candidate.right > reach:
            break
        if _PER_UNIT.search(f"{candidate.text} {token.text}"):
            return True
    return False


def _best_neighbour(
    tokens: list[OcrToken],
    label_index: int,
    spec: FieldSpec,
    strategy: str,
    all_aliases: set[str],
) -> tuple[OcrToken, normalizer.NormalizedValue] | None:
    """Closest plausible token in the given direction that normalizes cleanly."""
    label = tokens[label_index]
    scored: list[tuple[float, OcrToken, normalizer.NormalizedValue]] = []

    for index, candidate in enumerate(tokens):
        if index == label_index:
            continue

        if strategy == "same_line":
            if not _same_line(label, candidate) or candidate.x < label.right:
                continue
        else:
            if candidate.y < label.bottom or not _horizontally_aligned(label, candidate):
                continue

        if not _is_plausible(candidate.text, spec.value_type, all_aliases):
            continue

        if spec.value_type == "currency" and _unit_price_split_across_tokens(
            tokens, candidate
        ):
            continue

        text = candidate.text
        if spec.value_type == "code":
            text = _with_dropped_prefix(tokens, label, candidate, text)

        normalized = normalizer.normalize(
            text, spec.value_type, spec.date_precision
        )
        if not normalized.ok:
            continue
        if spec.value_type == "date" and not _plausible_pack_date(normalized.value):
            continue
        if spec.value_type == "currency" and not _plausible_price(normalized.value):
            continue

        score = _spatial_score(label, candidate, strategy)
        if score <= _MIN_SPATIAL_SCORE:
            continue
        scored.append((score, candidate, normalized))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    _, token, normalized = scored[0]
    return token, normalized
