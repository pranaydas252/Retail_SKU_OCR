"""The Android wire contract, pinned from both sides.

The app's models live in Kotlin and the server's in Pydantic, so nothing in
either language can notice when one side renames a key. The mismatch shows up
at runtime on the device, as a deserialization crash or a field that silently
reads null — the most expensive place in this project to find a typo.

So this test parses the real ApiModels.kt and checks it against the real
response models. It is deliberately not a copy of the field names: a copy would
have to be updated by the same person making the rename, which is exactly the
step that gets forgotten.

Direction matters, and it is not symmetric:

* The app sets ``ignoreUnknownKeys = true``, so a key the server adds is
  harmless. Extra server keys are not an error.
* A key the app declares and the server never sends is an error. Without a
  Kotlin default it throws ``MissingFieldException`` and the scan fails; with
  one it quietly reads as the default, which is worse because it looks like
  data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.models.request_models import ConfirmScanRequest
from app.models.response_models import (
    BoundingBox,
    ConfirmScanResponse,
    ExtractedField,
    HealthResponse,
    OcrTokenModel,
    PrintScanResponse,
    ScanResponse,
)

API_MODELS = (
    Path(__file__).resolve().parent.parent
    / "android"
    / "retail-ocr"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "markss"
    / "retailocr"
    / "data"
    / "ApiModels.kt"
)

#: Kotlin data class -> the Pydantic model it mirrors.
PAIRS: list[tuple[str, type[BaseModel]]] = [
    ("ScanResponse", ScanResponse),
    ("ExtractedField", ExtractedField),
    ("OcrToken", OcrTokenModel),
    ("BoundingBox", BoundingBox),
    ("ConfirmResponse", ConfirmScanResponse),
    ("PrintResponse", PrintScanResponse),
    ("HealthResponse", HealthResponse),
]

_CLASS = re.compile(
    r"@Serializable\s+data\s+class\s+(\w+)\s*\((?P<body>.*?)\n\)",
    re.DOTALL,
)

#: The start of a constructor property. `@SerialName("x")` wins over the Kotlin
#: name when present, because that is the key that actually goes on the wire.
_PROPERTY = re.compile(
    r"(?:@SerialName\(\"(?P<serial>[^\"]+)\"\)\s*)?"
    r"va[lr]\s+(?P<name>\w+)\s*:",
)


def _has_default(fragment: str) -> bool:
    """Does this property declaration carry a Kotlin default?

    The `=` has to be found at bracket depth zero. Reading the type with a
    naive `[^,=\\n]+` looks like it works and does not: `Map<String,
    ExtractedField> = emptyMap()` stops at the comma inside the generic and
    never sees its own default, so every defaulted collection reads as
    required.
    """
    depth = 0
    for char in fragment:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
        elif char == "=" and depth == 0:
            return True
    return False


def kotlin_classes() -> dict[str, dict[str, bool]]:
    """Parse ApiModels.kt into {class: {wireKey: hasDefault}}.

    Properties are located by `val <name>:` rather than by splitting the
    parameter list on commas, for the same nested-generic reason.
    """
    source = API_MODELS.read_text(encoding="utf-8")
    classes: dict[str, dict[str, bool]] = {}

    for match in _CLASS.finditer(source):
        body = match.group("body")
        starts = list(_PROPERTY.finditer(body))
        properties: dict[str, bool] = {}

        for index, prop in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
            key = prop.group("serial") or prop.group("name")
            properties[key] = _has_default(body[prop.end():end])

        classes[match.group(1)] = properties

    return classes


def emitted_keys(model: type[BaseModel]) -> set[str]:
    """Keys the server puts on the wire, i.e. the serialization aliases."""
    return {
        field.alias or name for name, field in model.model_fields.items()
    }


def always_present(model: type[BaseModel]) -> set[str]:
    """Keys present in every response.

    A field with a default still serializes — Pydantic emits it unless asked
    not to — so for this contract "always present" means every field.
    """
    return emitted_keys(model)


@pytest.fixture(scope="module")
def kotlin() -> dict[str, dict[str, bool]]:
    assert API_MODELS.exists(), (
        f"ApiModels.kt not found at {API_MODELS}. If the Android package moved, "
        "update API_MODELS — do not delete this test."
    )
    return kotlin_classes()


class TestParsing:
    """The parser itself, because a regex that silently matches nothing would
    turn every assertion below into a vacuous pass."""

    def test_every_expected_class_was_found(self, kotlin):
        missing = [name for name, _ in PAIRS if name not in kotlin]
        assert not missing, f"not parsed out of ApiModels.kt: {missing}"

    def test_classes_have_properties(self, kotlin):
        empty = [name for name, _ in PAIRS if not kotlin[name]]
        assert not empty, f"parsed with no properties: {empty}"

    def test_a_known_property_is_read_correctly(self, kotlin):
        # ScanResponse.scanId has no default; overallConfidence does. If the
        # parser cannot tell those apart, the required-field check below is
        # meaningless.
        assert kotlin["ScanResponse"]["scanId"] is False
        assert kotlin["ScanResponse"]["overallConfidence"] is True

    def test_a_default_behind_a_generic_is_read_correctly(self, kotlin):
        # `Map<String, ExtractedField> = emptyMap()`. The comma inside the type
        # argument is exactly what a naive parse trips over, and getting this
        # wrong makes the contract stricter than the app really is.
        assert kotlin["ScanResponse"]["fields"] is True
        assert kotlin["ScanResponse"]["timings"] is True
        assert kotlin["ConfirmResponse"]["validationNotes"] is True


class TestResponseContract:

    @pytest.mark.parametrize("kotlin_name,model", PAIRS, ids=[p[0] for p in PAIRS])
    def test_app_expects_nothing_the_server_does_not_send(
        self, kotlin, kotlin_name, model
    ):
        unknown = set(kotlin[kotlin_name]) - emitted_keys(model)
        assert not unknown, (
            f"{kotlin_name} declares {sorted(unknown)}, which "
            f"{model.__name__} never sends"
        )

    @pytest.mark.parametrize("kotlin_name,model", PAIRS, ids=[p[0] for p in PAIRS])
    def test_required_app_fields_are_always_sent(self, kotlin, kotlin_name, model):
        required = {k for k, has_default in kotlin[kotlin_name].items() if not has_default}
        missing = required - always_present(model)
        assert not missing, (
            f"{kotlin_name} requires {sorted(missing)} with no Kotlin default, "
            f"so a response without it throws MissingFieldException on the device"
        )


class TestRequestContract:
    """The one direction that runs app -> server."""

    def test_server_accepts_what_the_app_sends(self, kotlin):
        sent = set(kotlin["ConfirmRequest"])
        accepted = {
            field.alias or name
            for name, field in ConfirmScanRequest.model_fields.items()
        }
        unknown = sent - accepted
        assert not unknown, (
            f"ConfirmRequest sends {sorted(unknown)}, which ConfirmScanRequest "
            "does not accept"
        )

    def test_a_confirm_body_from_the_app_validates(self):
        # The literal shape ConfirmRequest serializes to, including an
        # explicitly null field — the operator asserting a value is absent from
        # the pack, which is information rather than a missing value.
        body = {
            "fields": {"batchNumber": "A23C91", "lotCode": None},
            "deviceId": "TC22-0001",
        }
        parsed = ConfirmScanRequest.model_validate(body)
        assert parsed.fields == {"batchNumber": "A23C91", "lotCode": None}
        assert parsed.device_id == "TC22-0001"


class TestStatusVocabulary:
    """Status and band strings are compared as literals on the device, so a
    server-side rename would fail open: `status == "COMPLETED"` simply stops
    matching and the app reports a generic error."""

    def test_kotlin_constants_exist_on_the_server(self):
        from app.models.response_models import ConfidenceBand, ProcessingStatus

        source = API_MODELS.read_text(encoding="utf-8")
        declared = set(re.findall(r'const val \w+ = "([A-Z_]+)"', source))

        known = (
            {s.value for s in ProcessingStatus}
            | {b.value for b in ConfidenceBand}
            # Set by the app when the operator edits a value; never sent by the
            # server, and stored as-is by the confirm endpoint.
            | {"OPERATOR"}
            # Field sources, which are plain strings on the server side.
            | {"OCR_RULES", "DERIVED_RULE", "NOT_FOUND"}
        )

        unknown = declared - known
        assert not unknown, (
            f"ApiModels.kt compares against {sorted(unknown)}, which the server "
            "never produces"
        )
