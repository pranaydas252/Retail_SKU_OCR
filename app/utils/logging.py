"""Structured logging and stage timing.

CLAUDE.md section 21 requires per-stage durations, scan identifiers, and
failure reasons in the logs, and forbids dumping images or credentials.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line.

    Extras attached with ``logger.info(msg, extra={...})`` are merged into the
    object so downstream log tooling can filter on scan_id or stage without
    parsing the message text.
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # PaddleOCR and PaddleX are extremely chatty at INFO, including per-request
    # HTTP traces from the model downloader. Their warnings still surface.
    for noisy in ("paddle", "paddlex", "paddleocr", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


class StageTimer:
    """Accumulates named stage durations in milliseconds for one scan."""

    def __init__(self) -> None:
        self.stages: dict[str, int] = {}
        self._start = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = int((time.perf_counter() - started) * 1000)

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def as_dict(self) -> dict[str, int]:
        return {**self.stages, "totalMs": self.total_ms}
