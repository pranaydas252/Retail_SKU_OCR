"""Tests for the PaddleOCR result adapter.

The conversion from PaddleOCR's 3.x result shape to OcrToken is the single
riskiest line of contact with the library, so it is tested against fixture
dictionaries rather than by running inference. No models, no network.
"""

from __future__ import annotations

import numpy as np

from app.config import Settings
from app.services.ocr_service import OcrService, OcrToken


def result(texts, scores, boxes=None, polys=None) -> dict:
    """A PaddleOCR 3.x result. Keys verified against paddleocr 3.4.1."""
    return {
        "rec_texts": texts,
        "rec_scores": scores,
        "rec_boxes": np.array(boxes) if boxes is not None else None,
        "rec_polys": polys,
    }


class TestTokenConversion:
    def test_converts_boxes_to_tokens(self):
        tokens = OcrService._to_tokens(
            result(["BATCH NO"], [0.96], boxes=[[19, 86, 370, 127]]), "original"
        )

        assert len(tokens) == 1
        token = tokens[0]
        assert token.text == "BATCH NO"
        assert token.confidence == 0.96
        assert (token.x, token.y, token.width, token.height) == (19, 86, 351, 41)
        assert token.variant == "original"

    def test_empty_result_returns_empty_list(self):
        assert OcrService._to_tokens(result([], []), None) == []

    def test_missing_keys_return_empty_list(self):
        assert OcrService._to_tokens({}, None) == []

    def test_falls_back_to_polys_when_boxes_absent(self):
        polys = [np.array([[19, 86], [370, 86], [370, 127], [19, 127]])]
        tokens = OcrService._to_tokens(result(["MFG"], [0.9], polys=polys), None)

        assert len(tokens) == 1
        assert (tokens[0].x, tokens[0].y) == (19, 86)
        assert (tokens[0].width, tokens[0].height) == (351, 41)

    def test_rotated_poly_yields_enclosing_box(self):
        # A skewed line has no axis-aligned box; take its extent.
        polys = [np.array([[10, 20], [100, 5], [110, 40], [20, 55]])]
        tokens = OcrService._to_tokens(result(["EXP"], [0.8], polys=polys), None)

        token = tokens[0]
        assert (token.x, token.y) == (10, 5)
        assert (token.right, token.bottom) == (110, 55)

    def test_token_without_geometry_is_skipped_not_fabricated(self):
        # Never invent a position — extraction would silently mis-associate.
        tokens = OcrService._to_tokens(result(["ORPHAN"], [0.9]), None)
        assert tokens == []

    def test_missing_score_defaults_to_zero(self):
        tokens = OcrService._to_tokens(
            result(["A", "B"], [0.9], boxes=[[0, 0, 10, 10], [0, 20, 10, 30]]), None
        )
        assert tokens[0].confidence == 0.9
        assert tokens[1].confidence == 0.0


class TestReadingOrder:
    def test_sorts_top_to_bottom_then_left_to_right(self):
        tokens = OcrService._to_tokens(
            result(
                ["THIRD", "FIRST", "SECOND"],
                [0.9, 0.9, 0.9],
                boxes=[[10, 200, 90, 230], [10, 10, 90, 40], [200, 12, 280, 42]],
            ),
            None,
        )

        assert [t.text for t in tokens] == ["FIRST", "SECOND", "THIRD"]

    def test_same_line_tokens_group_despite_small_y_offset(self):
        # Label and value printed on one line rarely share an exact top edge.
        # Section 9's same-line association depends on them staying adjacent.
        tokens = OcrService._to_tokens(
            result(
                ["A23C91", "BATCH NO"],
                [0.9, 0.9],
                boxes=[[300, 103, 400, 130], [20, 100, 200, 130]],
            ),
            None,
        )

        assert [t.text for t in tokens] == ["BATCH NO", "A23C91"]


class TestGeometry:
    def test_center_right_and_bottom(self):
        token = OcrToken("X", x=10, y=20, width=100, height=40, confidence=0.9)
        assert token.center == (60.0, 40.0)
        assert token.right == 110
        assert token.bottom == 60


class TestRotatedRescue:
    """The gate around re-reading a box rotated upright.

    PP-OCRv5 detects vertical text correctly but recognises it sideways. The
    rescue crops such a box and rotates it, but only under conditions worth
    two extra recognition passes. Those conditions are what is tested here;
    the recognition itself is stubbed, so no models and no network.
    """

    IMAGE = np.zeros((400, 400, 3), dtype=np.uint8)

    def service(self, rotated_text: str, rotated_confidence: float) -> OcrService:
        service = OcrService(Settings(ocr_retry_rotated_boxes=True))
        service._read_plain = lambda _image: (rotated_text, rotated_confidence)
        return service

    def test_tall_low_confidence_box_is_replaced(self):
        token = OcrToken("nokdaa", x=50, y=50, width=56, height=238, confidence=0.40)
        rescued = self.service("keep your", 0.92)._rescue_rotated(self.IMAGE, token)

        assert rescued.text == "keep your"
        assert rescued.confidence == 0.92

    def test_replacement_keeps_the_original_geometry(self):
        # Section 9 associates a label with its value by position. The text is
        # read from a rotated crop, but the box must keep describing where the
        # text sits on the pack.
        token = OcrToken("H", x=50, y=60, width=75, height=258, confidence=0.16)
        rescued = self.service("MADE IN BIHAR", 0.98)._rescue_rotated(self.IMAGE, token)

        assert (rescued.x, rescued.y) == (50, 60)
        assert (rescued.width, rescued.height) == (75, 258)

    def test_wide_box_is_left_alone(self):
        # Ordinary horizontal label text, however badly it read.
        token = OcrToken("Phm", x=10, y=10, width=200, height=40, confidence=0.05)
        rescued = self.service("rewritten", 0.99)._rescue_rotated(self.IMAGE, token)

        assert rescued is token

    def test_confident_tall_box_is_left_alone(self):
        # A tall box read confidently is usually one large character, not a
        # rotated line, and rereading it would waste two passes.
        token = OcrToken("7", x=10, y=10, width=40, height=200, confidence=0.97)
        rescued = self.service("rewritten", 0.99)._rescue_rotated(self.IMAGE, token)

        assert rescued is token

    def test_marginal_improvement_does_not_overwrite(self):
        # Without a margin, noise-level differences would rewrite correct text.
        token = OcrToken("ioom", x=10, y=10, width=40, height=200, confidence=0.51)
        rescued = self.service("ICOi", 0.55)._rescue_rotated(self.IMAGE, token)

        assert rescued is token

    def test_rescue_is_off_by_default(self):
        # Measured on the corpus: the rescue fires but wins no fields. It must
        # not turn itself on.
        assert Settings().ocr_retry_rotated_boxes is False
