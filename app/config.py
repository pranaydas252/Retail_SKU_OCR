"""Application configuration.

Single source of configuration truth (CLAUDE.md section 28). Nothing else in
the codebase may contain a literal path, URL, credential, or threshold.

Values come from environment variables, optionally via a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- API -------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = ""
    log_level: str = "INFO"

    # --- SQL Server ------------------------------------------------------
    sql_connection_string: str = ""

    # --- Image storage ---------------------------------------------------
    image_root: Path = PROJECT_ROOT / "images"
    debug_artifact_root: Path = PROJECT_ROOT / "debug"
    save_processed_variants: bool = False

    max_upload_bytes: int = 15 * 1024 * 1024
    min_image_width: int = 320
    min_image_height: int = 320

    # --- OCR -------------------------------------------------------------
    # Model names are configurable so all four PP-OCRv5 combinations can be
    # A/B tested without a code change.
    #
    # Defaults are mobile/mobile, chosen from a measurement on this hardware
    # rather than from published benchmarks: server detection took 12.23s
    # against mobile detection's 1.60s on the same image, and both produced
    # identical text. See PLAN.md section 1a.
    ocr_det_model_name: str = "PP-OCRv5_mobile_det"
    ocr_rec_model_name: str = "PP-OCRv5_mobile_rec"
    ocr_model_dir: Path = PROJECT_ROOT / "models"

    ocr_use_textline_orientation: bool = False
    ocr_use_doc_orientation_classify: bool = False
    ocr_use_doc_unwarping: bool = False

    ocr_det_limit_side_len: int = 960
    ocr_det_limit_type: str = "max"
    ocr_det_thresh: float = 0.3
    ocr_det_box_thresh: float = 0.6
    ocr_rec_score_thresh: float = 0.0

    # 1 is measured fastest on CPU, against intuition. Batched recognition
    # pads every crop to the widest in the batch, and label text varies wildly
    # in width, so batching spends most of its time on padding. Measured on a
    # 10-region label: batch 1 = 3.77s, batch 6 = 5.26s, batch 16 = 6.11s.
    ocr_rec_batch_size: int = 1

    # PaddlePaddle 3.3.1 on Windows CPU crashes inside its oneDNN backend
    # with "ConvertPirAttribute2RuntimeAttribute not support
    # [pir::ArrayAttribute<pir::DoubleAttribute>]" during text detection.
    # Disabling oneDNN is the workaround. Do not flip this to True without
    # re-testing detection end to end.
    ocr_enable_mkldnn: bool = False

    ocr_warmup_on_startup: bool = True

    # Preprocessing variants to run and compare, in priority order. Keep this
    # list short — CLAUDE.md section 7 forbids a combination explosion, and
    # each variant costs a full OCR pass.
    ocr_variants: str = "original"

    # --- Field extraction (Phase 2) --------------------------------------
    field_alias_config: Path = PROJECT_ROOT / "config" / "field_aliases.yaml"
    confidence_high_threshold: float = 0.95
    confidence_review_threshold: float = 0.80

    # --- Printer / QR ----------------------------------------------------
    printer_dpi: int = 203
    qr_size_mm: int = 10
    qr_magnification: int = 2
    qr_error_correction: str = "M"

    @property
    def variant_list(self) -> list[str]:
        """Preprocessing variants as a cleaned list."""
        return [v.strip() for v in self.ocr_variants.split(",") if v.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Import this, never construct Settings()."""
    return Settings()
