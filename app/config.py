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

    # Recognition floor for a token that a SPATIAL strategy proposes as a
    # field value. Deliberately not ocr_rec_score_thresh, which stays 0.0 so
    # every detected box is preserved for the audit trail (section 16).
    #
    # A low score is the recogniser reporting that it could not read the crop.
    # Letting such a token become a presented value is the false-accept path
    # section 24 ranks worst. On SCAN-000487 the MRP label matched, the real
    # value "85.00" sat one row up at 0.9974, and the "below" strategy instead
    # returned "20" at 0.2043 -- a tall vertical strip of edge print. The
    # operator was shown 20.00.
    #
    # Applies only to strategies that reach ACROSS tokens to guess a value.
    # An "inline" hit, where the label and value are the same token, is not
    # a guess and is exempt -- as are VLM tokens, which carry 0.0 by
    # construction because the runtime reports no per-token score.
    extract_min_value_confidence: float = 0.50

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

    # --- Rotated text rescue ---------------------------------------------
    #
    # PP-OCRv5 detects vertical text correctly but recognises it sideways.
    # Measured on SAMPLE_20260816_195846_975, cropping the detected box and
    # rotating it 90 degrees clockwise before re-reading:
    #
    #   as printed        rotated 90 cw
    #   ----------------  -------------------
    #   "ueep Ao"  0.54   "city clean"   0.97
    #   "8H30"     0.29   "MADEINBHAR"   0.98
    #   "nokdaa"   0.61   "keep your"    0.92
    #
    # use_textline_orientation is the built-in answer to this and it rescued
    # only one of those three, so the rescue is done explicitly instead.
    #
    # MEASURED ON THE CORPUS, AND IT WON NOTHING. It fires: 11 tokens were
    # rewritten across the 20 images, several from nothing to a confident read
    # ("" -> "CPMSGP90CN324C" at 1.00, "" -> "city clean" at 0.99, "0" at 0.19
    # -> "461080" at 1.00). But every rescued string is boilerplate - trademark
    # lines, care instructions, a pincode, "MADE IN BIHAR" - and not one is a
    # target field. Core stayed at 45%, codes at 29%, false accepts at 0, and
    # the run cost 0.3s/image more.
    #
    # It stays, off, because the capability is correct and Indian FMCG does
    # print dates vertically on pouches; the corpus simply has none. Do not
    # re-test it on this corpus expecting a different answer.
    ocr_retry_rotated_boxes: bool = False

    # A box taller than this multiple of its width is treated as possibly
    # vertical. Ordinary label text is wider than tall by a wide margin.
    ocr_rotated_aspect_ratio: float = 1.3

    # Only boxes the recogniser was already unsure about are retried. A tall
    # box read confidently is usually a single large character, not a rotated
    # line, and rereading it wastes two passes.
    ocr_rotated_max_confidence: float = 0.90

    # The rotated read must beat the original by this margin to replace it.
    # Without a margin, noise-level differences would rewrite correct text.
    ocr_rotated_min_gain: float = 0.15

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

    # When the second engine runs.
    #
    # Latency is the reason this is not simply "always". Measured on the
    # reference machine, one qwen2.5vl:3b pass took 25s in isolation and 99-120s
    # with PP-OCRv5 competing for the same CPU — against a 2.2s end-to-end scan
    # on a pack PP-OCRv5 handles. Paying two minutes on every scan to rescue the
    # minority of packs that need it would make the fast case unusable to save
    # the slow one.
    #
    #   fallback  run only when the primary engine came up short. Good packs
    #             stay at PP-OCRv5 speed; hard ones pay for what they need.
    #   always    run on every scan, so both engines answer and agreement is
    #             available on every field. For measurement, not for an aisle.
    #   never     equivalent to vlm_enabled = False
    vlm_trigger: str = "fallback"

    # Which engines run. Overridable per request so the device can A/B the two
    # pipelines against the same packs without a server restart.
    #
    #   both  ML Kit gates the capture, PP-OCRv5 reads, the VLM reads, and the
    #         two are merged with disagreements shown to the operator.
    #   vlm   ML Kit gates the capture and the VLM is the only reader.
    #         PP-OCRv5 does not run at all - not merely ignored, skipped, so
    #         the latency reflects the pipeline being measured.
    #
    # Both modes go through the SAME extraction, normalization, validation and
    # confidence code. That is what makes the comparison mean anything: a
    # difference in the numbers is a difference between the engines, not
    # between two sets of rules.
    engine_mode: str = "both"

    # What "came up short" means: fewer than this many of the three core fields
    # (MRP, manufacturing date, expiry date) extracted. Two of three is a pack
    # that mostly worked; one or none is the case worth two minutes of model.
    vlm_fallback_below_core_fields: int = 2
    # qwen3-vl:4b, replacing qwen2.5vl:3b on 2026-08-17.
    #
    # The 2.5 generation was chosen on measurement, and then measurement caught
    # it out: on the Eno carton it returned mfg 2023-04 and exp 2025-01 against
    # a truth of 2026-05 and 2028-04, and batch 526L289A against S26L289A. A
    # confidently wrong value is the worst failure class this project has
    # (section 24), so a plausible-looking misread is a reason to replace the
    # engine, not to tune around it.
    #
    #   engine                        core   Eno inkjet stamp
    #   qwen2.5vl:3b                   53%   plausible, and wrong
    #   PaddleOCR-VL-1.5-GGUF          36%   two lines, then stopped
    #   qwen3-vl:4b                     ?    to be measured
    #
    # The 53%/36% figures are from the previous corpus and the older extractor,
    # so both will move when the new corpus lands. PaddleOCR-VL stays installed
    # and configurable — it is five times faster, and if a future corpus says it
    # is enough, that is a switch of one setting.
    # NOT "qwen3-vl:4b". That tag is the Thinking variant: every token goes to
    # the `thinking` field, `response` comes back empty, and it stops on
    # `length` while still reasoning. `think: false` is ignored on both
    # /api/generate and /api/chat — the chat template forces it. It spent
    # 130-140s per image and never answered. The "-instruct" suffix is load
    # bearing.
    vlm_model: str = "qwen3-vl:4b-instruct"
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

    # Tuned by scripts/calibrate_confidence.py against the 19-image gated
    # corpus with BOTH engines running, which is what production does. The
    # previous values were measured on PP-OCRv5 alone, so the two-engine
    # agreement signal never fired while they were being chosen.
    #
    # HIGH was 0.92. On the two-engine corpus that admitted no wrong value but
    # reached only 11 of 51 correct ones, so 40 correct values arrived carrying
    # a warning -- and an operator warned on four values out of five stops
    # reading the warning. Worse, 0.92 was unreachable by construction: with a
    # realistic spatial score of 0.68 a flawless corroborated extraction
    # ceilings at 0.896.
    #
    # What actually separates right from wrong here is not the score, it is
    # engine agreement, and that is enforced structurally in confidence.band
    # rather than by this number. The threshold only decides how much of the
    # AGREED set qualifies, and on this corpus every agreed value is correct:
    #
    #   HIGH >= 0.60  31 of 51 correct, 0 wrong
    #   HIGH >= 0.75  31 of 51 correct, 0 wrong
    #   HIGH >= 0.80  25 of 51 correct, 0 wrong
    #
    # Coverage is flat from 0.60 to 0.75, so 0.75 is taken as the strictest
    # setting that costs nothing. It is a floor against a badly-scored value
    # that two engines happen to agree on -- not seen on this corpus, which is
    # why the threshold is cheap, and exactly why it is kept.
    #
    # REVIEW drops 0.80 -> 0.50 to stay below HIGH and because that is where
    # correctness falls off. The resulting bands finally order by correctness:
    # HIGH 100% (31 values), REVIEW 67% (15), LOW 45% (22). REVIEW and LOW are
    # still weakly separated and should be revisited as the corpus grows.
    confidence_high_threshold: float = 0.75
    confidence_review_threshold: float = 0.50

    # HIGH for a scan where only ONE engine ran, so the agreement rule above
    # has nothing to work with and cannot protect the band.
    #
    # Not the same number, because it is not the same claim. Measured on the
    # same corpus with PP-OCRv5 alone, 0.75 admits FOUR wrong values into HIGH
    # -- an expiry off by a day component, a batch read as "9ML", a
    # manufacturing date off by nine years, and an MRP of 31612.00 against a
    # printed 1012.50. 0.92 is the lowest threshold that admits none, reaching
    # 12 of 37 correct values.
    #
    # This path is live whenever vlm_trigger is "fallback" or "never", and
    # whenever Ollama is unreachable, so it is not a hypothetical.
    confidence_high_threshold_single_engine: float = 0.92

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
