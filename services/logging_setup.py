"""Structured logging: context (tenant, run, stage, correlation id), JSON lines to disk, and a Seq shipper.

Every log record from the `news_pipeline` logger carries the current tenant_id,
run_id, stage, and correlation_id from context variables. Handlers:

- console: the existing human-readable line (left to logging.basicConfig)
- file: JSON lines at LOG_DIR/app.jsonl (rotating), always on
- Seq: CLEF batches to SEQ_LOGGING_BASE_URL with SEQ_LOGGING_API_KEY; a no-op when unset

`configure_logging()` is idempotent and is called by the API lifespan and the MCP main.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import queue
import threading
import time
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER_NAME = "news_pipeline"
APP_NAME = "news-generator"

tenant_id_var: ContextVar[Optional[int]] = ContextVar("log_tenant_id", default=None)
tenant_slug_var: ContextVar[Optional[str]] = ContextVar("log_tenant_slug", default=None)
run_id_var: ContextVar[Optional[str]] = ContextVar("log_run_id", default=None)
stage_var: ContextVar[Optional[str]] = ContextVar("log_stage", default=None)
correlation_id_var: ContextVar[Optional[str]] = ContextVar("log_correlation_id", default=None)
client_ip_var: ContextVar[Optional[str]] = ContextVar("log_client_ip", default=None)

CONTEXT_FIELDS = ("tenant_id", "tenant_slug", "run_id", "stage", "correlation_id", "client_ip")
_STANDARD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "", (), None))) | {"message", "asctime"}

_configured = False
_seq_handler: Optional["SeqHandler"] = None


class ContextFilter(logging.Filter):
    """Attach the context variables to every record (explicit `extra` values win)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field, var in (
            ("tenant_id", tenant_id_var), ("tenant_slug", tenant_slug_var), ("run_id", run_id_var),
            ("stage", stage_var), ("correlation_id", correlation_id_var), ("client_ip", client_ip_var),
        ):
            if getattr(record, field, None) is None:
                setattr(record, field, var.get())
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS and not k.startswith("_") and v is not None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app": APP_NAME,
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str)


class SeqHandler(logging.Handler):
    """Batches records and ships them to Seq's raw CLEF ingestion endpoint from a background thread."""

    LEVELS = {"DEBUG": "Debug", "INFO": "Information", "WARNING": "Warning", "ERROR": "Error", "CRITICAL": "Fatal"}

    def __init__(self, base_url: str, api_key: str, batch_size: int = 100, flush_seconds: float = 2.0) -> None:
        super().__init__()
        self.url = f"{base_url.rstrip('/')}/api/events/raw?clef"
        self.api_key = api_key
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.queue: "queue.Queue[str]" = queue.Queue(maxsize=10000)
        self.dropped = 0
        self.failures = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="seq-shipper", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        event: dict[str, Any] = {
            "@t": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "@mt": record.getMessage().replace("{", "{{").replace("}", "}}"),
            "@l": self.LEVELS.get(record.levelname, "Information"),
            "logger": record.name,
            "app": APP_NAME,
        }
        event.update(_extras(record))
        if record.exc_info:
            event["@x"] = "".join(traceback.format_exception(*record.exc_info))
        try:
            self.queue.put_nowait(json.dumps(event, default=str))
        except queue.Full:
            self.dropped += 1

    def _drain(self, block: bool) -> list[str]:
        items: list[str] = []
        try:
            items.append(self.queue.get(timeout=self.flush_seconds) if block else self.queue.get_nowait())
            while len(items) < self.batch_size:
                items.append(self.queue.get_nowait())
        except queue.Empty:
            pass
        return items

    def _send(self, items: list[str]) -> None:
        if not items:
            return
        import requests

        try:
            response = requests.post(
                self.url,
                data=("\n".join(items) + "\n").encode("utf-8"),
                headers={"X-Seq-ApiKey": self.api_key, "Content-Type": "application/vnd.serilog.clef"},
                timeout=5,
            )
            if response.status_code >= 300:
                self.failures += 1
                if self.failures in (1, 10, 100):
                    logging.getLogger(__name__).warning("Seq ingestion returned %s: %s", response.status_code, response.text[:120])
        except Exception as exc:  # noqa: BLE001 - logging must never raise
            self.failures += 1
            if self.failures in (1, 10, 100):
                logging.getLogger(__name__).warning("Seq ingestion failed (%d so far): %s", self.failures, exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._send(self._drain(block=True))
        self._send(self._drain(block=False))

    def flush(self) -> None:
        deadline = time.monotonic() + 3
        while not self.queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    def close(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self._thread.join(timeout=5)
        super().close()


def log_dir() -> Path:
    root = Path(os.environ.get("LOG_DIR", "logs")).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def configure_logging() -> dict[str, Any]:
    """Attach context, JSON file, and Seq handlers to the app logger. Safe to call more than once."""
    global _configured, _seq_handler
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return {"file": str(log_dir() / "app.jsonl"), "seq": bool(_seq_handler)}
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(level)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    context = ContextFilter()
    logger.addFilter(context)

    file_handler = logging.handlers.RotatingFileHandler(log_dir() / "app.jsonl", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(context)
    logger.addHandler(file_handler)

    base_url = os.environ.get("SEQ_LOGGING_BASE_URL", "").strip()
    api_key = os.environ.get("SEQ_LOGGING_API_KEY", "").strip()
    if base_url and api_key:
        _seq_handler = SeqHandler(base_url, api_key)
        _seq_handler.addFilter(context)
        logger.addHandler(_seq_handler)
        logger.info("[logging] Seq shipping enabled to %s", base_url)
    else:
        logger.info("[logging] Seq shipping disabled (set SEQ_LOGGING_BASE_URL and SEQ_LOGGING_API_KEY)")
    _configured = True
    return {"file": str(log_dir() / "app.jsonl"), "seq": bool(_seq_handler)}


def flush() -> None:
    if _seq_handler is not None:
        _seq_handler.flush()


def seq_stats() -> dict[str, Any]:
    if _seq_handler is None:
        return {"enabled": False}
    return {"enabled": True, "queued": _seq_handler.queue.qsize(), "dropped": _seq_handler.dropped, "failures": _seq_handler.failures}
