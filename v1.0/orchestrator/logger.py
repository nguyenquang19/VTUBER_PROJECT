"""Logger: structlog console + JSONL sink (ARCHITECTURE 9.3, Phase 0 task 4).

Hai stream tách biệt:
- `events.jsonl`  — log hệ thống chung (state transition, toggle, error)
- `turns.jsonl`   — 1 dòng / turn hội thoại, schema ARCHITECTURE 9.3

Log rotation theo size (13.8), config từ `config/logging.yaml`.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
_LOG_SESSION_ID: ContextVar[str | None] = ContextVar("log_session_id", default=None)


def bind_log_session(session_id: str | None) -> None:
    """Bind the active runtime session to JSONL records in the current async context."""
    _LOG_SESSION_ID.set(session_id)


class JsonlWriter:
    """Append-only JSONL writer với rotation theo size.

    Thread-safe. Rotation: `turns.jsonl` → `turns.jsonl.1` → ... → `.N`,
    file cũ nhất bị xoá khi vượt keep_files.
    """

    def __init__(
        self,
        path: Path,
        max_size_mb: int = 100,
        keep_files: int = 5,
        rotation_enabled: bool = True,
        source: str | None = None,
        degraded_buffer_records: int = 256,
        error_callback: Any = None,
    ) -> None:
        if degraded_buffer_records <= 0:
            raise ValueError("degraded_buffer_records must be positive")
        self._path = Path(path)
        self._max_bytes = max_size_mb * 1024 * 1024
        self._keep = keep_files
        self._rotation_enabled = rotation_enabled
        self._source = source or self._path.stem
        self._lock = threading.Lock()
        self._buffer: deque[str] = deque(maxlen=int(degraded_buffer_records))
        self._error_callback = error_callback
        self._errors_total = 0
        self._buffer_dropped_total = 0
        self._writes_total = 0
        self._degraded = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._record_error(exc)

    def _should_rotate(self) -> bool:
        if not self._rotation_enabled:
            return False
        try:
            return self._path.stat().st_size >= self._max_bytes
        except FileNotFoundError:
            return False

    def _rotate(self) -> None:
        oldest = self._path.with_suffix(self._path.suffix + f".{self._keep}")
        if oldest.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive = self._path.with_suffix(self._path.suffix + f".archive.{stamp}")
            counter = 1
            while archive.exists():
                archive = self._path.with_suffix(
                    self._path.suffix + f".archive.{stamp}.{counter}"
                )
                counter += 1
            oldest.rename(archive)
        for i in range(self._keep - 1, 0, -1):
            src = self._path.with_suffix(self._path.suffix + f".{i}")
            if src.exists():
                src.rename(self._path.with_suffix(self._path.suffix + f".{i + 1}"))
        if self._path.exists():
            self._path.rename(self._path.with_suffix(self._path.suffix + ".1"))

    def write(self, record: dict[str, Any]) -> bool:
        enriched = _sanitize_json_record(record)
        legacy_ts = enriched.pop("ts", None)
        enriched.setdefault("schema_version", 1)
        enriched["timestamp"] = _utc_iso(enriched.get("timestamp") or legacy_ts)
        enriched.setdefault("source", self._source)
        enriched.setdefault("session_id", _LOG_SESSION_ID.get())
        line = json.dumps(enriched, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                if self._should_rotate():
                    self._rotate()
                with self._path.open("a", encoding="utf-8") as f:
                    while self._buffer:
                        f.write(self._buffer.popleft() + "\n")
                    f.write(line + "\n")
                self._writes_total += 1
                self._degraded = False
                return True
            except OSError as exc:
                if len(self._buffer) == self._buffer.maxlen:
                    self._buffer_dropped_total += 1
                self._buffer.append(line)
                self._record_error(exc)
                return False

    def flush(self) -> bool:
        """Retry buffered records without raising into the runtime."""
        with self._lock:
            if not self._buffer:
                return True
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as file:
                    while self._buffer:
                        file.write(self._buffer.popleft() + "\n")
                self._degraded = False
                return True
            except OSError as exc:
                self._record_error(exc)
                return False

    def get_metrics(self) -> dict[str, Any]:
        return {
            "logging_sink_errors_total": self._errors_total,
            "logging_sink_writes_total": self._writes_total,
            "logging_sink_buffered_records": len(self._buffer),
            "logging_sink_buffer_dropped_total": self._buffer_dropped_total,
            "logging_sink_degraded": self._degraded,
        }

    def _record_error(self, exc: OSError) -> None:
        self._errors_total += 1
        self._degraded = True
        if callable(self._error_callback):
            try:
                self._error_callback(self._source, type(exc).__name__)
            except Exception:
                pass


class TurnLogger:
    """Ghi turn hội thoại vào turns.jsonl theo schema ARCHITECTURE 9.3."""

    def __init__(self, writer: JsonlWriter) -> None:
        self._writer = writer

    def log_turn(self, turn: dict[str, Any]) -> None:
        record = dict(turn)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        self._writer.write(record)

    def get_metrics(self) -> dict[str, Any]:
        return self._writer.get_metrics()

    def flush(self) -> bool:
        return self._writer.flush()


_turn_logger: TurnLogger | None = None
_events_writer: JsonlWriter | None = None
_configured = False


def _jsonl_sink(writer: JsonlWriter):
    """structlog processor cuối chain: ghi event dict vào JSONL rồi trả lại dict."""

    def processor(logger, method_name, event_dict):
        writer.write(dict(event_dict))
        return event_dict

    return processor


def setup_logging(
    level: str = "INFO",
    console_enabled: bool = True,
    console_colors: bool = True,
    jsonl_enabled: bool = True,
    log_dir: str | Path = "logs",
    events_file: str = "events.jsonl",
    turns_file: str = "turns.jsonl",
    rotation_enabled: bool = True,
    max_size_mb: int = 100,
    keep_files: int = 5,
    degraded_buffer_records: int = 256,
    metrics: Any = None,
) -> TurnLogger:
    """Cấu hình structlog + JSONL. Trả về TurnLogger cho turn hội thoại.

    Idempotent: gọi lại sẽ reconfigure (dùng khi config hot-reload đổi level).
    """
    global _turn_logger, _events_writer, _configured

    log_dir = Path(log_dir)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if jsonl_enabled:
        error_callback = (
            metrics.record_logging_sink_error
            if metrics is not None and hasattr(metrics, "record_logging_sink_error") else None
        )
        events_writer = JsonlWriter(
            log_dir / events_file,
            max_size_mb=max_size_mb,
            keep_files=keep_files,
            rotation_enabled=rotation_enabled,
            source="events",
            degraded_buffer_records=degraded_buffer_records,
            error_callback=error_callback,
        )
        _events_writer = events_writer
        processors.append(_jsonl_sink(events_writer))
    else:
        _events_writer = None

    if console_enabled:
        processors.append(structlog.dev.ConsoleRenderer(colors=console_colors))
    else:
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS.get(level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )

    turns_writer = JsonlWriter(
        log_dir / turns_file,
        max_size_mb=max_size_mb,
        keep_files=keep_files,
        rotation_enabled=rotation_enabled,
        source="turn",
        degraded_buffer_records=degraded_buffer_records,
        error_callback=(
            metrics.record_logging_sink_error
            if metrics is not None and hasattr(metrics, "record_logging_sink_error") else None
        ),
    )
    _turn_logger = TurnLogger(turns_writer)
    _configured = True
    return _turn_logger


def setup_from_config(loader, metrics: Any = None) -> TurnLogger:
    """Cấu hình logger từ ConfigLoader (config/logging.yaml)."""
    from services.data.sanitize import configure_hash_salt

    configure_hash_salt(loader.get(
        "data_privacy", "privacy.viewer_hash_salt_file", "data/privacy_salt.bin",
    ))
    return setup_logging(
        level=loader.get("logging", "level", "INFO"),
        console_enabled=loader.get("logging", "console.enabled", True),
        console_colors=loader.get("logging", "console.colors", True),
        jsonl_enabled=loader.get("logging", "jsonl.enabled", True),
        log_dir=loader.get("logging", "jsonl.dir", "logs"),
        events_file=loader.get("logging", "jsonl.events_file", "events.jsonl"),
        turns_file=loader.get("logging", "jsonl.turns_file", "turns.jsonl"),
        rotation_enabled=loader.get("logging", "rotation.enabled", True),
        max_size_mb=loader.get("logging", "rotation.max_size_mb", 100),
        keep_files=loader.get("logging", "rotation.keep_files", 5),
        degraded_buffer_records=loader.get(
            "logging", "fail_safe.buffer_records", 256,
        ),
        metrics=metrics,
    )


def get_logger(name: str | None = None):
    """Lấy structlog logger. Tự setup mặc định nếu chưa configure."""
    if not _configured:
        setup_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def get_turn_logger() -> TurnLogger:
    if _turn_logger is None:
        return setup_logging()
    return _turn_logger


def flush_logging() -> None:
    """Flush stdlib handlers; JSONL writers already fs-close every append."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            continue
    if _events_writer is not None:
        _events_writer.flush()
    if _turn_logger is not None:
        _turn_logger.flush()


def logging_snapshot() -> dict[str, Any]:
    return {
        "events": _events_writer.get_metrics() if _events_writer is not None else None,
        "turns": _turn_logger.get_metrics() if _turn_logger is not None else None,
    }


def _utc_iso(value: Any = None) -> str:
    """Normalize timestamps to an explicit UTC ISO-8601 value."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _sanitize_json_record(record: dict[str, Any]) -> dict[str, Any]:
    """Scrub free-form strings before any local JSONL write."""
    from services.data.sanitize import hash_viewer_id, mask_pii

    protected = {"event", "incident_id", "kind", "request_id", "schema_version",
                 "session_id", "source", "timestamp", "turn_id", "ts"}

    def sanitize(value: Any, key: str | None = None) -> Any:
        if key == "viewer_id" and isinstance(value, str):
            return value if value.startswith("v_") else hash_viewer_id(value)
        if isinstance(value, str):
            return value if key in protected else mask_pii(value)
        if isinstance(value, dict):
            return {nested_key: sanitize(nested, nested_key)
                    for nested_key, nested in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item) for item in value]
        return value

    return {key: sanitize(value, key) for key, value in record.items()}
