"""Fail-closed emergency latch for speech and environment action boundaries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from interfaces.base import HealthStatus
from interfaces.operations import EmergencyControlService


AsyncAction = Callable[[], Awaitable[None]]


async def _noop() -> None:
    return None


class EmergencyController(EmergencyControlService):
    service_id = "emergency_controller"

    def __init__(
        self,
        *,
        pause_actions: AsyncAction,
        resume_actions: AsyncAction,
        cancel_speech: AsyncAction | None = None,
        cancel_environment_actions: AsyncAction | None = None,
        prune_stale_work: AsyncAction | None = None,
        pause_recovery: Callable[[str], None] | None = None,
        resume_recovery: Callable[[], None] | None = None,
        audit: Callable[[str, str, str], None] | None = None,
        metrics: Any = None,
        reason_max_chars: int = 240,
    ) -> None:
        self._pause_actions = pause_actions
        self._resume_actions = resume_actions
        self._cancel_speech = cancel_speech or _noop
        self._cancel_environment_actions = cancel_environment_actions or _noop
        self._prune_stale_work = prune_stale_work or _noop
        self._pause_recovery = pause_recovery
        self._resume_recovery = resume_recovery
        self._audit = audit
        self._metrics = metrics
        self._reason_max_chars = max(1, int(reason_max_chars))
        self._latched = False
        self._running = False
        self._reason = ""
        self._triggered_at: str | None = None
        self._trigger_count = 0
        self._resume_count = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, latched=self._latched)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "emergency_stop_latched": self._latched,
            "emergency_stop_triggered_total": self._trigger_count,
            "emergency_stop_resumed_total": self._resume_count,
        }

    async def trigger(self, reason: str = "emergency stop") -> bool:
        clean = " ".join(str(reason).split())[:self._reason_max_chars] or "emergency stop"
        async with self._lock:
            if self._latched:
                self._record("emergency_stop", "already_latched")
                return True
            self._latched = True
            self._reason = clean
            self._triggered_at = datetime.now(timezone.utc).isoformat()
            self._trigger_count += 1
            if self._pause_recovery is not None:
                self._pause_recovery("emergency_stop")
            results = await asyncio.gather(
                self._pause_actions(), self._cancel_speech(),
                self._cancel_environment_actions(), return_exceptions=True,
            )
            outcome = "completed" if not any(isinstance(v, Exception) for v in results) else "degraded"
            self._record("emergency_stop", outcome)
            return outcome == "completed"

    async def resume(self, reason: str = "operator resume") -> bool:
        del reason
        async with self._lock:
            if not self._latched:
                self._record("emergency_resume", "already_running")
                return True
            try:
                await self._prune_stale_work()
                await self._resume_actions()
            except Exception:
                self._record("emergency_resume", "failed")
                return False
            self._latched = False
            self._reason = ""
            self._triggered_at = None
            self._resume_count += 1
            if self._resume_recovery is not None:
                self._resume_recovery()
            self._record("emergency_resume", "completed")
            return True

    def permits_speech(self) -> bool:
        return self._running and not self._latched

    def permits_environment_action(self) -> bool:
        return self._running and not self._latched

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self._running, "latched": self._latched,
            "reason": self._reason, "triggered_at": self._triggered_at,
            "speech_permitted": self.permits_speech(),
            "environment_action_permitted": self.permits_environment_action(),
            "trigger_count": self._trigger_count, "resume_count": self._resume_count,
        }

    def _record(self, action: str, outcome: str) -> None:
        if self._audit is not None:
            self._audit(action, "agent", outcome)
        if self._metrics is not None and hasattr(self._metrics, "record_emergency_control"):
            self._metrics.record_emergency_control(action, outcome)
