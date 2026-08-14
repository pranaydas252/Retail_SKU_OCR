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

    if value_type == "date":
        return bool(_DATE_HINT.search(text))
    if value_type == "currency":
        return bool(_NUMBER_HINT.search(text))
    if value_type == "code":
        return bool(_CODE_HINT.search(text))
    return True


# --- spatial association ---------------------------------------------------


def _same_line(label: OcrToken, candidate: OcrToken) -> bool:
    """Vertical overlap, tolerant of the few pixels label and value differ by."""
    overlap = min(label.bottom, candidate.bottom) - max(label.y, candidate.y)
    shorter = min(label.height, candidate.height) or 1
    return overlap > 0.5 * shorter


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


def extract_fields(tokens: list[OcrToken]) -> dict[str, FieldCandidate]:
    """Extract the best candidate per field from a token list."""
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
    return results


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
