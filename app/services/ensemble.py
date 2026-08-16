"""Merging two recognition engines, and using their agreement as evidence.

PP-OCRv5 and the vision-language model fail on different things. PP-OCRv5 is
fast and accurate on clearly printed text and cannot read inkjet stamps at all;
the VLM reads stamps and is slower and looser everywhere else. Neither is a
superset of the other — measured on the earlier corpus, 23 of 50 values were
answered by exactly one of the two.

That makes agreement worth more than either engine's own confidence. Two
independent recognisers arriving at the same string is hard to do by accident,
and the earlier ensemble measurement bears it out: merging only where they
agreed gave 2 wrong values against 14 for a naive union. Section 12 already
anticipates this — it lists "agreement between OCR preprocessing variants" as a
confidence signal, and two engines is the same idea with more independence.

So this module does not pick a winner and hide the loser. It reports what each
engine said, whether they agreed, and what the disagreement was, and lets
confidence scoring decide what that is worth. A value both engines produced
should reach the operator differently from a value only one produced, and both
should reach them differently from a value they contradicted each other on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.field_extractor import FieldCandidate

logger = logging.getLogger(__name__)

#: Engine identifiers, carried through to the API so a scan can be diagnosed.
PRIMARY = "OCR"
SECONDARY = "VLM"


@dataclass(frozen=True)
class MergedField:
    """One field after both engines have had their say."""

    candidate: FieldCandidate
    #: Which engines produced a usable value for this field.
    engines: tuple[str, ...]
    #: True when both produced the same value.
    agreed: bool
    #: What the other engine said, when they disagreed. None otherwise.
    conflict: str | None = None

    @property
    def corroborated(self) -> bool:
        return self.agreed and len(self.engines) > 1

    @property
    def contested(self) -> bool:
        return self.conflict is not None


def same_value(left: str | None, right: str | None) -> bool:
    """Compare two normalized values, ignoring separators and case.

    "2026-07" and "2026/07" are the same reading, and two engines should not be
    recorded as disagreeing because one of them wrote a slash. Deliberately the
    same comparison the accuracy harness uses, so "agreed" here means what
    "correct" means there.
    """
    if left is None or right is None:
        return False

    def key(value: str) -> str:
        return "".join(ch for ch in value.upper() if ch.isalnum())

    return key(left) == key(right)


def merge(
    primary: dict[str, FieldCandidate],
    secondary: dict[str, FieldCandidate],
) -> dict[str, MergedField]:
    """Combine two engines' extractions into one result per field.

    The primary engine wins a contested field. That is not a claim it is more
    often right — it is that changing which engine answers a field depending on
    who disagreed would make the result impossible to reason about, and the
    contest is recorded so confidence can push it into review instead. An
    operator looking at a flagged field is the intended resolution, not a
    tie-break rule nobody can predict.

    A field only the secondary engine found is taken. That is the entire reason
    it is running: on packs PP-OCRv5 cannot read, it is the only engine with an
    answer.
    """
    merged: dict[str, MergedField] = {}

    for name in set(primary) | set(secondary):
        first = primary.get(name)
        second = secondary.get(name)

        if first is not None and second is not None:
            if same_value(first.normalized.value, second.normalized.value):
                merged[name] = MergedField(
                    candidate=first,
                    engines=(PRIMARY, SECONDARY),
                    agreed=True,
                )
            else:
                merged[name] = MergedField(
                    candidate=first,
                    engines=(PRIMARY, SECONDARY),
                    agreed=False,
                    conflict=second.normalized.value,
                )
        elif first is not None:
            merged[name] = MergedField(first, engines=(PRIMARY,), agreed=False)
        elif second is not None:
            merged[name] = MergedField(second, engines=(SECONDARY,), agreed=False)

    logger.info(
        "Engines merged",
        extra={
            "agreed": sorted(n for n, m in merged.items() if m.corroborated),
            "contested": sorted(n for n, m in merged.items() if m.contested),
            "primaryOnly": sorted(
                n for n, m in merged.items() if m.engines == (PRIMARY,)
            ),
            "secondaryOnly": sorted(
                n for n, m in merged.items() if m.engines == (SECONDARY,)
            ),
        },
    )
    return merged
