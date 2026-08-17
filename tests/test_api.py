"""API contract tests.

OCR is stubbed. These assert the HTTP contract the Android app codes against —
status codes, the alias-cased JSON shape, and error bodies — not recognition
quality. No models are loaded, so the suite stays fast.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import scan_service
from app.services.ocr_service import OcrToken


@pytest.fixture
def client(monkeypatch):
    # Neutralize startup: no model download, no database round trip.
    monkeypatch.setattr("app.main.check_health", lambda: False)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: __import__("app.config", fromlist=["Settings"]).Settings(
            ocr_warmup_on_startup=False, api_key=""
        ),
    )
    with TestClient(app) as test_client:
        yield test_client


def jpeg(width: int = 640, height: int = 480) -> bytes:
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    return cv2.imencode(".jpg", image)[1].tobytes()


def stub_tokens(monkeypatch, tokens: list[OcrToken]):
    class StubOcr:
        is_ready = True

        def recognize(self, image, variant=None):
            return [
                OcrToken(
                    t.text, t.x, t.y, t.width, t.height, t.confidence, variant
                )
                for t in tokens
            ]

    monkeypatch.setattr(scan_service, "get_ocr_service", lambda: StubOcr())


class TestHealth:
    def test_health_is_up(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "UP"

    def test_health_reports_configured_models(self, client):
        body = client.get("/api/v1/health").json()
        assert "detectionModel" in body
        assert "recognitionModel" in body

    def test_health_needs_no_api_key(self, client):
        # IIS and monitoring must be able to probe it.
        assert client.get("/api/v1/health").status_code == 200


class TestCreateScan:
    def test_returns_tokens_in_camel_case(self, client, monkeypatch):
        stub_tokens(
            monkeypatch,
            [OcrToken("BATCH NO", 20, 100, 180, 30, 0.97),
             OcrToken("A23C91", 300, 100, 100, 30, 0.99)],
        )

        response = client.post(
            "/api/v1/scans", files={"image": ("label.jpg", jpeg(), "image/jpeg")}
        )

        assert response.status_code == 200
        body = response.json()
        # Kotlin data classes are generated against these exact keys.
        assert body["status"] == "COMPLETED"
        assert body["scanId"].startswith("SCAN-")
        assert "overallConfidence" in body
        assert "variantUsed" in body
        assert len(body["tokens"]) == 2
        assert body["tokens"][0]["bbox"] == {
            "x": 20, "y": 100, "width": 180, "height": 30,
        }

    def test_no_text_is_success_not_error(self, client, monkeypatch):
        # The operator is asked to recapture; this is not a 500 (section 22).
        stub_tokens(monkeypatch, [])

        response = client.post(
            "/api/v1/scans", files={"image": ("label.jpg", jpeg(), "image/jpeg")}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "NO_TEXT_DETECTED"
        assert body["overallConfidence"] is None
        assert body["message"]

    def test_unreadable_image_returns_400(self, client):
        response = client.post(
            "/api/v1/scans",
            files={"image": ("label.jpg", b"definitely not a jpeg", "image/jpeg")},
        )

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "UNREADABLE_IMAGE"

    def test_undersized_image_returns_400(self, client):
        response = client.post(
            "/api/v1/scans",
            files={"image": ("label.jpg", jpeg(64, 64), "image/jpeg")},
        )

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "IMAGE_TOO_SMALL"

    def test_missing_file_returns_422(self, client):
        assert client.post("/api/v1/scans").status_code == 422

    def test_extracted_fields_are_returned(self, client, monkeypatch):
        stub_tokens(monkeypatch, [OcrToken("MRP Rs. 245.00", 10, 10, 300, 20, 0.9)])

        body = client.post(
            "/api/v1/scans", files={"image": ("l.jpg", jpeg(), "image/jpeg")}
        ).json()

        assert body["fields"]["mrp"]["value"] == "245.00"
        assert body["fields"]["mrp"]["band"] in {"HIGH", "REVIEW", "LOW"}
        assert body["fields"]["mrp"]["source"] == "OCR_RULES"

    def test_missing_fields_are_present_with_null_values(self, client, monkeypatch):
        # The UI must render an explicit empty box, not silently omit the
        # field (CLAUDE.md section 11).
        stub_tokens(monkeypatch, [OcrToken("MRP Rs. 245.00", 10, 10, 300, 20, 0.9)])

        fields = client.post(
            "/api/v1/scans", files={"image": ("l.jpg", jpeg(), "image/jpeg")}
        ).json()["fields"]

        assert fields["batchNumber"]["value"] is None
        assert fields["batchNumber"]["source"] == "NOT_FOUND"
        assert {"batchNumber", "manufacturingDate", "expiryDate", "lotCode", "mrp"} <= set(fields)

    def test_timings_are_reported(self, client, monkeypatch):
        stub_tokens(monkeypatch, [OcrToken("X", 0, 0, 10, 10, 0.9)])

        timings = client.post(
            "/api/v1/scans", files={"image": ("l.jpg", jpeg(), "image/jpeg")}
        ).json()["timings"]

        assert {"validateMs", "storeMs", "ocrMs", "totalMs"} <= set(timings)


class TestSecondEngineCannotFakeSuccess:
    """The VLM earns COMPLETED by producing a field, not by producing output.

    It never declines. On the 20-image corpus it missed 4 values where
    PP-OCRv5 missed 29, and it answers a blank page as readily as a label —
    enabling it turned a white test image into a COMPLETED scan carrying zero
    fields, which sends the operator to an empty confirmation screen instead of
    asking for a recapture.
    """

    def test_vlm_output_without_a_field_is_still_no_text(self, client, monkeypatch):
        # PP-OCRv5 finds nothing; the VLM emits text that yields no field.
        stub_tokens(monkeypatch, [])
        monkeypatch.setattr(
            scan_service, "should_run_vlm", lambda tokens, settings: True
        )

        class Chatty:
            def tokens(self, image):
                return [OcrToken("a blank white page", 0, 0, 10, 10, 0.9)]

        monkeypatch.setattr(scan_service, "get_vlm_service", lambda: Chatty())

        response = client.post(
            "/api/v1/scans", files={"image": ("blank.jpg", jpeg(), "image/jpeg")}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "NO_TEXT_DETECTED"

    def test_a_field_from_the_vlm_alone_does_count(self, client, monkeypatch):
        # The case the second engine exists for: PP-OCRv5 read nothing and the
        # VLM read the stamp. Telling the operator to recapture here would
        # discard a good result.
        stub_tokens(monkeypatch, [])
        monkeypatch.setattr(
            scan_service, "should_run_vlm", lambda tokens, settings: True
        )

        class Reader:
            def tokens(self, image):
                return [
                    OcrToken("MFG", 10, 10, 60, 20, 0.9),
                    OcrToken("07/2026", 90, 10, 90, 20, 0.9),
                ]

        monkeypatch.setattr(scan_service, "get_vlm_service", lambda: Reader())

        response = client.post(
            "/api/v1/scans", files={"image": ("label.jpg", jpeg(), "image/jpeg")}
        )

        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["fields"]["manufacturingDate"]["value"] == "2026-07"
        # And it is capped, because nothing corroborated it (PLAN.md F2).
        assert body["fields"]["manufacturingDate"]["band"] != "HIGH"
