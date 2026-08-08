"""Audited operator pause/resume controls and action-queue projection."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from interfaces.base import HealthStatus
from interfaces.operations import OperatorControlService
from orchestrator.logger import JsonlWriter


ControlAction = Callable[[], Awaitable[None]]
QueueProvider = Callable[[], list[dict[str, Any]]]


class RuntimeControlPlane(OperatorControlService):
    service_id = "runtime_control_plane"

    def __init__(
        self,
        *,
        pause_action: ControlAction,
        resume_action: ControlAction,
        queue_provider: QueueProvider,
        audit_path: str | Path,
        metrics: Any = None,
        audit_limit: int = 50,
    ) -> None:
        self._pause_action = pause_action
        self._resume_action = resume_action
        self._queue_provider = queue_provider
        self._writer = JsonlWriter(Path(audit_path), source="operator_audit")
        self._metrics = metrics
        self._audit = deque(maxlen=max(1, int(audit_limit)))
        self._running = False
        self._paused = False
        self._pause_reason = ""

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, paused=self._paused)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "operator_control_paused": self._paused,
            "operator_control_audit_entries": len(self._audit),
        }

    @property
    def paused(self) -> bool:
        return self._paused

    async def pause(self, reason: str) -> bool:
        clean = " ".join(str(reason).split())[:240] or "operator pause"
        if self._paused:
            self.record_operator_action("pause", "agent", "already_paused")
            return True
        await self._pause_action()
        self._paused = True
        self._pause_reason = clean
        self.record_operator_action("pause", "agent", "completed")
        return True

    async def resume(self, reason: str) -> bool:
        if not self._paused:
            self.record_operator_action("resume", "agent", "already_running")
            return True
        await self._resume_action()
        self._paused = False
        self._pause_reason = ""
        self.record_operator_action("resume", "agent", "completed")
        return True

    def record_operator_action(self, action: str, target: str, outcome: str) -> None:
        from services.data.sanitize import mask_pii

        record = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": (mask_pii(" ".join(str(action).split())[:80]) or ""),
            "target": (mask_pii(" ".join(str(target).split())[:120]) or ""),
            "outcome": (mask_pii(" ".join(str(outcome).split())[:80]) or ""),
        }
        self._audit.append(record)
        self._writer.write(record)
        if self._metrics is not None and hasattr(self._metrics, "record_operator_control"):
            self._metrics.record_operator_control(record["action"], record["outcome"])

    def snapshot(self) -> dict[str, Any]:
        try:
            queue = list(self._queue_provider())
        except Exception:
            queue = []
        return {
            "available": self._running,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "action_queue": queue,
            "audit": list(self._audit),
        }
