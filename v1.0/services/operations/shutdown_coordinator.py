"""Ordered, idempotent graceful shutdown with an atomic runtime snapshot."""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.operations import RecoveryAction, ShutdownCoordinatorService


SnapshotProvider = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class ShutdownStep:
    name: str
    callback: RecoveryAction


class ShutdownCoordinator(ShutdownCoordinatorService):
    service_id = "shutdown_coordinator"

    def __init__(
        self,
        *,
        timeout_s: float,
        snapshot_path: str | Path,
        snapshot_provider: SnapshotProvider,
        flush_callback: Callable[[], Any] | None = None,
        metrics: Any = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("shutdown timeout must be positive")
        self.timeout_s = float(timeout_s)
        self.snapshot_path = Path(snapshot_path)
        self._snapshot_provider = snapshot_provider
        self._flush_callback = flush_callback
        self._metrics = metrics
        self._steps: list[ShutdownStep] = []
        self._running = False
        self._completed = False
        self._lock = asyncio.Lock()
        self._report: dict[str, Any] | None = None

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        snapshot_provider: SnapshotProvider,
        flush_callback: Callable[[], Any] | None = None,
        metrics: Any = None,
    ) -> "ShutdownCoordinator":
        return cls(
            timeout_s=float(loader.get("operations", "shutdown.timeout_s", 15.0)),
            snapshot_path=loader.get(
                "operations", "shutdown.snapshot_file",
                "logs/operations/last_runtime_snapshot.json",
            ),
            snapshot_provider=snapshot_provider,
            flush_callback=flush_callback,
            metrics=metrics,
        )

    def register_step(self, name: str, callback: RecoveryAction) -> None:
        clean = " ".join(str(name).split())
        if not clean or any(step.name == clean for step in self._steps):
            raise ValueError("shutdown step name must be non-empty and unique")
        self._steps.append(ShutdownStep(clean, callback))

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        await self.shutdown()

    async def health_check(self) -> HealthStatus:
        if self._completed:
            return HealthStatus.stopped(self.service_id)
        if self._running:
            return HealthStatus.healthy(self.service_id, steps=len(self._steps))
        return HealthStatus.stopped(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        report = self._report or {}
        return {
            "shutdown_completed": self._completed,
            "shutdown_steps_total": len(self._steps),
            "shutdown_step_errors": len(report.get("errors") or []),
        }

    async def shutdown(self) -> dict[str, Any]:
        async with self._lock:
            if self._report is not None:
                return dict(self._report)
            self._running = False
            started = datetime.now(timezone.utc)
            completed_steps: list[str] = []
            errors: list[dict[str, str]] = []
            for step in self._steps:
                try:
                    await asyncio.wait_for(step.callback(), timeout=self.timeout_s)
                    completed_steps.append(step.name)
                    self._record(step.name, "completed")
                except asyncio.TimeoutError:
                    errors.append({"step": step.name, "error": "timeout"})
                    self._record(step.name, "timeout")
                except Exception as exc:
                    errors.append({"step": step.name, "error": type(exc).__name__})
                    self._record(step.name, "failed")
            snapshot_error = self._write_snapshot(errors)
            if snapshot_error is not None:
                errors.append({"step": "snapshot", "error": snapshot_error})
                self._record("snapshot", "failed")
            else:
                completed_steps.append("snapshot")
                self._record("snapshot", "completed")
            flush_error = await self._flush()
            if flush_error is not None:
                errors.append({"step": "flush_logging", "error": flush_error})
                self._record("flush_logging", "failed")
            else:
                completed_steps.append("flush_logging")
                self._record("flush_logging", "completed")
            self._completed = True
            self._report = {
                "schema_version": 1,
                "status": "completed" if not errors else "completed_with_errors",
                "started_at": started.isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "completed_steps": completed_steps,
                "errors": errors,
                "snapshot_path": str(self.snapshot_path),
            }
            return dict(self._report)

    def _write_snapshot(self, errors: list[dict[str, str]]) -> str | None:
        try:
            value = {
                "schema_version": 1,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "shutdown_errors_before_snapshot": list(errors),
                **self._snapshot_provider(),
            }
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.snapshot_path)
            return None
        except Exception as exc:
            return type(exc).__name__

    async def _flush(self) -> str | None:
        if self._flush_callback is None:
            return None
        try:
            value = self._flush_callback()
            if inspect.isawaitable(value):
                await asyncio.wait_for(value, timeout=self.timeout_s)
            return None
        except Exception as exc:
            return type(exc).__name__

    def _record(self, step: str, outcome: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_shutdown_step"):
            self._metrics.record_shutdown_step(step, outcome)
