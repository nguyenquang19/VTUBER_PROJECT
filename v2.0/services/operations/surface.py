"""Single live operations snapshot and allowlisted command surface."""
from __future__ import annotations

import inspect
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.operations import (
    OperationsCommand,
    OperationsCommandResult,
    OperationsSnapshot,
    OperationsSurfaceService,
)


SnapshotProvider = Callable[[], Any | Awaitable[Any]]
CommandHandler = Callable[[Mapping[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True)
class OperationsSurfaceConfig:
    max_snapshot_sections: int
    max_commands: int
    max_label_chars: int
    max_payload_bytes: int

    @classmethod
    def from_loader(cls, loader: Any) -> "OperationsSurfaceConfig":
        base = "surface"
        return cls(
            max_snapshot_sections=_positive_int(
                loader.get("operations", f"{base}.max_snapshot_sections", 64),
                "surface.max_snapshot_sections",
            ),
            max_commands=_positive_int(
                loader.get("operations", f"{base}.max_commands", 32),
                "surface.max_commands",
            ),
            max_label_chars=_positive_int(
                loader.get("operations", f"{base}.max_label_chars", 120),
                "surface.max_label_chars",
            ),
            max_payload_bytes=_positive_int(
                loader.get("operations", f"{base}.max_payload_bytes", 16384),
                "surface.max_payload_bytes",
            ),
        )


class OperationsSurface(OperationsSurfaceService):
    """Observe and route explicit operator intent without soft policy authority."""

    service_id = "operations_surface"

    def __init__(
        self,
        config: OperationsSurfaceConfig,
        *,
        metrics: Any = None,
    ) -> None:
        if not isinstance(config, OperationsSurfaceConfig):
            raise ValueError("config must be OperationsSurfaceConfig")
        self.config = config
        self._metrics = metrics
        self._snapshot_providers: OrderedDict[str, SnapshotProvider] = OrderedDict()
        self._command_handlers: dict[str, CommandHandler] = {}
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "OperationsSurface":
        return cls(OperationsSurfaceConfig.from_loader(loader), **kwargs)

    def register_snapshot_provider(self, name: str, provider: SnapshotProvider) -> None:
        key = self._label(name, "snapshot provider")
        if not callable(provider):
            raise ValueError("snapshot provider must be callable")
        if key not in self._snapshot_providers and (
            len(self._snapshot_providers) >= self.config.max_snapshot_sections
        ):
            raise ValueError("operations snapshot provider capacity reached")
        self._snapshot_providers[key] = provider

    def register_command(self, name: str, handler: CommandHandler) -> None:
        key = self._label(name, "command")
        if not callable(handler):
            raise ValueError("command handler must be callable")
        if key not in self._command_handlers and len(self._command_handlers) >= self.config.max_commands:
            raise ValueError("operations command capacity reached")
        self._command_handlers[key] = handler

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            snapshot_providers=len(self._snapshot_providers),
            commands=len(self._command_handlers),
        )

    async def snapshot(self) -> OperationsSnapshot:
        sections: dict[str, Any] = {}
        failures: dict[str, str] = {}
        for name, provider in tuple(self._snapshot_providers.items()):
            try:
                sections[name] = await self.snapshot_section(name)
            except Exception as exc:
                failures[name] = type(exc).__name__
        if failures:
            sections["operations_degraded"] = {
                "failed_sections": dict(sorted(failures.items())),
            }
        self._record("snapshot")
        return OperationsSnapshot(
            schema_version=1,
            generated_at=datetime.now(timezone.utc),
            sections=sections,
        )

    async def snapshot_section(self, name: str) -> Any:
        key = self._label(name, "snapshot provider")
        provider = self._snapshot_providers.get(key)
        if provider is None:
            self._record("snapshot_section_missing")
            raise KeyError(key)
        try:
            value = provider()
            if inspect.isawaitable(value):
                value = await value
            result = _json_safe(value)
        except Exception:
            self._record(f"snapshot_failed:{key}")
            raise
        self._record(f"snapshot_section:{key}")
        return result

    async def execute(self, command: OperationsCommand) -> OperationsCommandResult:
        if not isinstance(command, OperationsCommand):
            raise ValueError("command must be OperationsCommand")
        name = self._label(command.name, "command")
        try:
            self._validate_payload(command.payload)
        except ValueError as exc:
            self._record("command_rejected:invalid_payload")
            return OperationsCommandResult(
                command.command_id, False, 400,
                {"ok": False, "reason": str(exc)},
            )
        if not self._running:
            self._record("command_rejected:surface_stopped")
            return OperationsCommandResult(
                command.command_id, False, 503,
                {"ok": False, "reason": "operations_surface_stopped"},
            )
        handler = self._command_handlers.get(name)
        if handler is None:
            self._record("command_rejected:not_allowlisted")
            return OperationsCommandResult(
                command.command_id, False, 404,
                {"ok": False, "reason": "command_not_allowlisted"},
            )
        try:
            value = handler(command.payload)
            if inspect.isawaitable(value):
                value = await value
            status_code, payload = _command_result(value)
        except (ValueError, KeyError) as exc:
            self._record(f"command_failed:{name}")
            return OperationsCommandResult(
                command.command_id, False, 400,
                {"ok": False, "reason": str(exc) or type(exc).__name__},
            )
        except Exception as exc:
            self._record(f"command_failed:{name}")
            return OperationsCommandResult(
                command.command_id, False, 500,
                {"ok": False, "reason": type(exc).__name__},
            )
        accepted = 200 <= status_code < 300 and payload.get("ok", True) is not False
        self._record(f"command:{name}:{'accepted' if accepted else 'rejected'}")
        return OperationsCommandResult(
            command.command_id, accepted, status_code, _json_safe(payload),
        )

    def prometheus_text(self) -> bytes:
        if self._metrics is None or not hasattr(self._metrics, "prometheus_text"):
            return b""
        try:
            return self._metrics.prometheus_text()
        except Exception:
            self._record("metrics_exposition_failed")
            return b""

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"operations_surface_{name.replace(':', '_')}_total": count
            for name, count in sorted(self._counts.items())
        }

    def _label(self, value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"operations {name} must be non-empty")
        text = value.strip()
        if len(text) > self.config.max_label_chars:
            raise ValueError(f"operations {name} exceeds configured bound")
        return text

    def _validate_payload(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("operations command payload must be a mapping")
        rendered = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        if len(rendered) > self.config.max_payload_bytes:
            raise ValueError("operations command payload exceeds configured bound")

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _command_result(value: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(value, tuple) and len(value) == 2:
        status, payload = value
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValueError("operations command status must be an integer")
        if not isinstance(payload, Mapping):
            raise ValueError("operations command payload must be a mapping")
        return status, dict(payload)
    if isinstance(value, Mapping):
        return 200, dict(value)
    raise ValueError("operations command handler returned an invalid result")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
