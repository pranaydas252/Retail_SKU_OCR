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

    # --- Vision-language model (Phase 7) ---------------------------------
    #
    # A second recognition engine, for the print PP-OCRv5 cannot read at all.
    # Measured on a TC22 capture of an Eno carton: the printed field names came
    # back at 0.95-0.99 confidence and the inkjet values beside them at
    # 0.05-0.53, as "Phm", "Voow", "JAND". Three PP-OCRv5 recognition models
    # were tried and none read them. That gap is not closable with rules.
    #
    # Off by default. Nothing about the existing pipeline changes until this is
    # measured against a corpus, and the corpus is being recollected.
    vlm_enabled: bool = False
    vlm_base_url: str = "http://127.0.0.1:11434"
    # qwen2.5vl:3b, chosen on measurement rather than on name.
    #
    #   engine                        core   Eno inkjet stamp
    #   qwen2.5vl:3b                   53%   read it
    #   PaddleOCR-VL-1.5-GGUF          36%   two lines, then stopped
    #
    # The 53%/36% figures are from the previous corpus and the older extractor,
    # so both will move when the new corpus lands; the ordering is what matters
    # and it is consistent with the per-pack result. PaddleOCR-VL stays
    # installed and configurable — it is five times faster, and if a future
    # corpus says it is enough, that is a switch of one setting.
    vlm_model: str = "qwen2.5vl:3b"
    vlm_timeout_seconds: int = 300

    # How the model is asked, which is a property of the model and not a
    # preference. Measured 2026-08-16: PaddleOCR-VL ignores an instruction to
    # return JSON entirely — it is a transcription model and answers by reading
    # the image aloud, returning "The Manager, GSK Consumer Relations..." to a
    # prompt demanding a JSON object. qwen2.5vl follows instructions and can be
    # asked for fields directly.
    #
    #   transcribe  the model reads the image; our own extractor finds the
    #               fields in the text, exactly as it does for PP-OCRv5
    #   json        the model reports the fields itself
    vlm_mode: str = "transcribe"
    vlm_prompt_transcribe: Path = (
        PROJECT_ROOT / "config" / "vlm_prompts" / "transcribe.txt"
    )
    vlm_prompt_json: Path = (
        PROJECT_ROOT / "config" / "vlm_prompts" / "fields_compact.txt"
    )

    @property
    def vlm_prompt(self) -> Path:
        return (
            self.vlm_prompt_json if self.vlm_mode == "json"
            else self.vlm_prompt_transcribe
        )

    # Deliberately NOT ocr_det_limit_side_len. That 960 is PP-OCRv5's detector
    # limit; a vision-language model has no detector and reads the whole image
    # at a fixed token budget, so the two have no reason to share a number.
    # 1024 is what the per-pack results above were measured at. Above it the
    # cost climbs steeply for no gain seen so far: the same pack took 14.4s at
    # 768, 29.5s at 1024 and over 100s at 1600 on one model.
    vlm_max_side: int = 1024

    # Guards against degenerate generation, both measured. Vision models on
    # dense label text fall into repeat loops - emitting "1.00 2.00 3.00..."
    # or LaTeX - and a capped prediction length with a repeat penalty took one
    # model from 25.3s to 3.3s per image with no loss of real output.
    vlm_max_tokens: int = 300
    vlm_repeat_penalty: float = 1.15
    vlm_temperature: float = 0.0

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
