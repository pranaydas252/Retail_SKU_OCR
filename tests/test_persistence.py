"""Persistence and confirm-flow tests (sections 13, 16, 22).

The database is stubbed. These assert the behaviour around persistence — what
happens on failure, what gets normalized on the way in, what the QR payload
looks like — not that SQL Server works.
"""

from __future__ import annotations

import pytest

from app.db.repositories import PersistenceError
from app.models.response_models import ProcessingStatus
from app.services import scan_service
from app.services.normalizer import normalize_date


class FakeRepo:
    """Records calls instead of writing to SQL Server."""

    def __init__(self, fail_on: str | None = None, unknown_scan: bool = False):
        self.fail_on = fail_on
        self.unknown_scan = unknown_scan
        self.saved: list[dict] = []
        self.confirmed: list[tuple] = []

    def new_scan_id(self) -> str:
        return "00000000-0000-0000-0000-000000000001"

    def save_scan(self, **kwargs):
        if self.fail_on == "save":
            raise PersistenceError("database is down")
        self.saved.append(kwargs)

    def confirm_scan(self, scan_code, confirmed, notes):
        if self.unknown_scan:
            raise PersistenceError(f"Unknown scan {scan_code}")
        if self.fail_on == "confirm":
            raise PersistenceError("database is down")
        self.confirmed.append((scan_code, confirmed, notes))
        return "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(scan_service, "repositories", fake)
    return fake


class TestConfirmNormalization:
    def test_normalizes_raw_operator_input(self, repo):
        scan_service.confirm_scan("SCAN-000001", {"manufacturingDate": "07/26"})

        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["manufacturingDate"] == "2026-07"

    def test_already_normalized_values_pass_through_unflagged(self, repo):
        # The app sends back the ISO value the server gave it. Re-normalizing
        # must be a no-op, not a fresh parse that reads 2026 as a month.
        response = scan_service.confirm_scan(
            "SCAN-000001",
            {"manufacturingDate": "2026-07", "expiryDate": "2028-06"},
        )

        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["manufacturingDate"] == "2026-07"
        assert confirmed["expiryDate"] == "2028-06"
        assert response.validation_notes == {}, "valid values must not be flagged"

    def test_operator_input_is_kept_when_it_cannot_be_normalized(self, repo):
        # Losing a correction because it failed a format rule would be the
        # worst outcome; the operator is the final authority.
        scan_service.confirm_scan("SCAN-000001", {"batchNumber": "!!!"})

        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["batchNumber"] == "!!!"

    def test_deliberate_null_is_not_flagged_as_not_found(self, repo):
        # Clearing a field asserts the label lacks it. That is information,
        # not a validation problem.
        response = scan_service.confirm_scan("SCAN-000001", {"lotCode": None})

        assert "lotCode" not in response.validation_notes
        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["lotCode"] is None

    def test_blank_string_is_treated_as_cleared(self, repo):
        scan_service.confirm_scan("SCAN-000001", {"lotCode": "   "})

        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["lotCode"] is None

    def test_unknown_field_is_stored_not_dropped(self, repo):
        response = scan_service.confirm_scan("SCAN-000001", {"gtin": "8901234567890"})

        _, confirmed, _ = repo.confirmed[0]
        assert confirmed["gtin"] == "8901234567890"
        assert "gtin" in response.validation_notes

    def test_inconsistent_dates_are_recorded_but_still_committed(self, repo):
        response = scan_service.confirm_scan(
            "SCAN-000001",
            {"manufacturingDate": "2028-06", "expiryDate": "2026-07"},
        )

        assert repo.confirmed, "operator input must still be committed"
        assert response.validation_notes["expiryDate"]

    def test_status_is_confirmed(self, repo):
        response = scan_service.confirm_scan("SCAN-000001", {"mrp": "245.00"})
        assert response.status == ProcessingStatus.CONFIRMED


class TestQrPayload:
    def test_payload_is_positional_and_uppercase(self):
        payload = scan_service.build_qr_payload(
            "SCAN-000001",
            {
                "skuCode": "8901234567890",
                "batchNumber": "a23c91",
                "manufacturingDate": "2026-07",
                "expiryDate": "2028-06",
                "lotCode": "k18p2",
                "mrp": "245.00",
            },
        )

        # SKU leads: it is the only value in the payload that was decoded
        # rather than recognised, and the only one a downstream system joins on.
        assert payload == (
            "8901234567890|SCAN-000001|A23C91|2026-07|2028-06|K18P2|245.00"
        )

    def test_a_skipped_barcode_leaves_the_sku_position_empty(self):
        # The barcode step can be skipped, and a pack scanned without one must
        # still print a payload a scanner can parse positionally.
        payload = scan_service.build_qr_payload(
            "SCAN-000002", {"batchNumber": "A23C91"}
        )

        assert payload.startswith("|SCAN-000002|")
        assert payload.split("|")[0] == ""

    def test_missing_fields_keep_their_position(self):
        # A parser on the scanner side must not have to guess which field is
        # absent.
        payload = scan_service.build_qr_payload(
            "SCAN-000001", {"batchNumber": "A23C91", "mrp": "20.00"}
        )

        assert payload.split("|") == [
            "", "SCAN-000001", "A23C91", "", "", "", "20.00",
        ]

    def test_payload_fits_the_10mm_density_budget(self):
        # Section 17: QR version 5 at magnification 2, error correction M,
        # gives roughly 154 alphanumeric characters.
        payload = scan_service.build_qr_payload(
            "SCAN-999999",
            {
                "skuCode": "8901234567890",
                "batchNumber": "A23C91XYZ",
                "manufacturingDate": "2026-07-25",
                "expiryDate": "2028-06-30",
                "lotCode": "K18P2ABC",
                "mrp": "12345.00",
            },
        )

        assert len(payload) <= 154


class TestPersistenceFailure:
    def test_scan_survives_a_database_outage(self, monkeypatch):
        # Several seconds of OCR the operator already waited for must not be
        # discarded because a write failed.
        import cv2
        import numpy as np

        fake = FakeRepo(fail_on="save")
        monkeypatch.setattr(scan_service, "repositories", fake)
        monkeypatch.setattr(scan_service, "next_scan_code", lambda: "SCAN-000001")

        class StubOcr:
            def recognize(self, image, variant=None):
                return []

        monkeypatch.setattr(scan_service, "get_ocr_service", lambda: StubOcr())

        image = np.full((480, 640, 3), 255, dtype=np.uint8)
        data = cv2.imencode(".jpg", image)[1].tobytes()

        response = scan_service.process_scan(data)

        assert response.persisted is False, "the client must know it was not stored"
        assert response.status == ProcessingStatus.NO_TEXT_DETECTED

    def test_confirm_failure_propagates(self, monkeypatch):
        # Confirm is the commit point. Reporting success without storing the
        # operator's confirmation is the worst outcome available.
        fake = FakeRepo(fail_on="confirm")
        monkeypatch.setattr(scan_service, "repositories", fake)

        with pytest.raises(PersistenceError):
            scan_service.confirm_scan("SCAN-000001", {"mrp": "245.00"})

    def test_unknown_scan_propagates(self, monkeypatch):
        fake = FakeRepo(unknown_scan=True)
        monkeypatch.setattr(scan_service, "repositories", fake)

        with pytest.raises(PersistenceError, match="Unknown scan"):
            scan_service.confirm_scan("SCAN-999999", {"mrp": "245.00"})


class TestNormalizationIdempotence:
    @pytest.mark.parametrize("raw", ["07/26", "25/07/2026", "JAN 2026", "07/2026"])
    def test_normalizing_twice_changes_nothing(self, raw):
        once = normalize_date(raw).value
        assert once is not None
        assert normalize_date(once).value == once

    def test_iso_month_is_not_reparsed_as_month_first(self):
        result = normalize_date("2026-07")
        assert result.value == "2026-07"
        assert result.notes == []

    def test_invalid_iso_is_still_rejected(self):
        assert normalize_date("2026-13").value is None
        assert normalize_date("2026-02-31").value is None


class TestQrPayloadAuthority:
    """The operator's confirmation wins over OCR (sections 4 and 11)."""

    def test_cleared_field_stays_empty_in_the_payload(self):
        # A field the operator deliberately cleared must not be repopulated
        # from OCR. Doing so prints data onto the label that the operator
        # removed on purpose.
        payload = scan_service.build_qr_payload(
            "SCAN-000047",
            {
                "skuCode": "8901234567890",
                "batchNumber": "A23C92",
                "manufacturingDate": "2026-07",
                "expiryDate": "2028-06",
                "lotCode": None,
                "mrp": "245.00",
            },
        )

        assert payload == (
            "8901234567890|SCAN-000047|A23C92|2026-07|2028-06||245.00"
        )
        assert payload.split("|")[5] == "", "cleared lot code must stay blank"
