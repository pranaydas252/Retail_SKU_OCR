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
from dataclasses import dataclass
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
        return bool(_NUMBER_HINT.search(text))
    if value_type == "code":
        if not _CODE_HINT.search(text):
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
    label_centre = label.y + label.height / 2
    candidate_centre = candidate.y + candidate.height / 2
    tallest = max(label.height, candidate.height) or 1
    return abs(label_centre - candidate_centre) < 0.8 * tallest


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
        return max(0.0, 1.0 - gap / 30.0)

    gap = max(0, candidate.y - label.bottom) / unit
    # Below-label association is inherently weaker than same-line, so it is
    # capped lower even at zero distance.
    return max(0.0, 0.85 - gap / 10.0)


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
                if normalized.ok:
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

    _derive_relative_expiry(tokens, results)
    _infer_unlabelled(tokens, results)
    return results


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
_PRICE_TOKEN = re.compile(
    r"(?:RS\.?|INR|₹|MRP)\s*:?\s*([0-9][0-9,]*(?:\.\d{1,2})?)"
    r"|([0-9][0-9,]*(?:\.\d{1,2})?)\s*/-"
    # A bare decimal, for stamps that print no currency marker at all. Kept
    # last and still filtered by the per-unit strip and the plausible-price
    # range, or every weight on the pack would look like a price.
    r"|([0-9]{1,5}\.[0-9]{2})",
    re.IGNORECASE,
)

#: Per-unit price. Never the MRP.
_PER_UNIT = re.compile(r"[0-9.]+\s*(?:/|PER\s*)\s*(?:G|GM|GRAM|ML|L|KG|N|PC)\b", re.IGNORECASE)


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

    dates: list[tuple[str, OcrToken]] = []
    prices: list[tuple[str, OcrToken]] = []

    for token in tokens:
        text = _split_run_together_dates(token.text)
        if _canonical(text) in _BOILERPLATE:
            continue

        without_unit = _PER_UNIT.sub(" ", text)

        for match in _PRICE_TOKEN.finditer(without_unit):
            value = match.group(1) or match.group(2) or match.group(3)
            # A retail pack price sits in a sane range. Outside it the number
            # is a weight, a licence number or a phone number.
            if value and _numeric(value) and 1.0 <= float(value.replace(",", "")) <= 100000:
                prices.append((value, token))

        for match in _DATE_TOKEN.finditer(without_unit):
            dates.append((match.group(1), token))

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

    parsed: list[tuple[str, str, OcrToken]] = []
    for raw, token in dates:
        normalized = normalizer.normalize_date(raw, "day")
        if normalized.ok and normalized.value:
            parsed.append((normalized.value, raw, token))

    # Distinct dates only: the same stamp read twice is not two dates.
    seen: set[str] = set()
    unique = [p for p in parsed if not (p[0] in seen or seen.add(p[0]))]
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


#: Two dates printed with no gap between them. Inkjet stamps run fields
#: together often enough that OCR returns "22/06/2621/03/27" as one token, and
#: a greedy date match then reads the year as 2621.
_RUN_TOGETHER = re.compile(
    r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2})(?=\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2})"
)


def _split_run_together_dates(text: str) -> str:
    """Insert a separator between two dates printed with no gap."""
    return _RUN_TOGETHER.sub(r" ", text)


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

        normalized = normalizer.normalize(
            candidate.text, spec.value_type, spec.date_precision
        )
        if not normalized.ok:
            continue

        scored.append((_spatial_score(label, candidate, strategy), candidate, normalized))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    _, token, normalized = scored[0]
    return token, normalized
