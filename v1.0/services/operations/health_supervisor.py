"""Bounded health recovery with backoff and per-service circuit breakers."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from interfaces.base import HealthState, HealthStatus
from interfaces.operations import HealthCheck, HealthSupervisorService, RecoveryAction
from orchestrator.logger import get_logger


@dataclass(frozen=True)
class SupervisorPolicy:
    recovery_mode: str = "bounded_auto_restart"
    interval_s: float = 10.0
    check_timeout_s: float = 5.0
    restart_timeout_s: float = 30.0
    unhealthy_threshold: int = 2
    max_restart_attempts: int = 3
    restart_window_s: float = 300.0
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 30.0

    def __post_init__(self) -> None:
        if self.recovery_mode not in {"bounded_auto_restart", "alert_only"}:
            raise ValueError("invalid health supervisor recovery mode")
        numeric = (
            self.interval_s, self.check_timeout_s, self.restart_timeout_s,
            self.restart_window_s, self.backoff_initial_s, self.backoff_max_s,
        )
        if min(numeric) <= 0 or self.unhealthy_threshold < 1 or self.max_restart_attempts < 1:
            raise ValueError("health supervisor policy values must be positive")

    @classmethod
    def from_loader(cls, loader: Any) -> "SupervisorPolicy":
        prefix = "health_supervisor"
        return cls(
            recovery_mode=str(loader.get("operations", f"{prefix}.recovery_mode")),
            interval_s=float(loader.get("operations", f"{prefix}.interval_s")),
            check_timeout_s=float(loader.get("operations", f"{prefix}.check_timeout_s")),
            restart_timeout_s=float(loader.get("operations", f"{prefix}.restart_timeout_s")),
            unhealthy_threshold=int(loader.get("operations", f"{prefix}.unhealthy_threshold")),
            max_restart_attempts=int(loader.get("operations", f"{prefix}.max_restart_attempts")),
            restart_window_s=float(loader.get("operations", f"{prefix}.restart_window_s")),
            backoff_initial_s=float(loader.get("operations", f"{prefix}.backoff_initial_s")),
            backoff_max_s=float(loader.get("operations", f"{prefix}.backoff_max_s")),
        )


@dataclass
class _Target:
    service_id: str
    check: HealthCheck
    restart: RecoveryAction | None
    consecutive_failures: int = 0
    restart_attempts: deque[float] = field(default_factory=deque)
    restarts_total: int = 0
    restart_failures_total: int = 0
    next_retry_at: float = 0.0
    circuit_open: bool = False
    last_status: HealthStatus | None = None
    last_action: str = "registered"


class HealthSupervisor(HealthSupervisorService):
    service_id = "live_health_supervisor"

    def __init__(
        self,
        policy: SupervisorPolicy,
        *,
        metrics: Any = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.policy = policy
        self._metrics = metrics
        self._clock = clock
        self._targets: dict[str, _Target] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._recovery_paused_reason: str | None = None
        self._rounds = 0
        self._log = get_logger("health_supervisor")

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None) -> "HealthSupervisor":
        return cls(SupervisorPolicy.from_loader(loader), metrics=metrics)

    def register_target(
        self,
        service_id: str,
        check: HealthCheck,
        restart: RecoveryAction | None = None,
    ) -> None:
        if not service_id.strip():
            raise ValueError("health target requires service id")
        self._targets[service_id] = _Target(service_id, check, restart)

    def unregister_target(self, service_id: str) -> None:
        self._targets.pop(service_id, None)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="live_health_supervisor")

    async def stop(self) -> None:
        self._running = False
        self.pause_recovery("shutdown")
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=self.policy.interval_s + 1)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        self._task = None
        self._stop_event = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        open_targets = sorted(
            target.service_id for target in self._targets.values() if target.circuit_open
        )
        if open_targets:
            return HealthStatus.degraded(
                self.service_id, "recovery circuit open", targets=open_targets,
            )
        return HealthStatus.healthy(
            self.service_id, targets=len(self._targets), rounds=self._rounds,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "health_supervisor_rounds": self._rounds,
            "health_supervisor_targets": len(self._targets),
            "health_supervisor_circuits_open": sum(
                1 for target in self._targets.values() if target.circuit_open
            ),
            "health_supervisor_restarts_total": sum(
                target.restarts_total for target in self._targets.values()
            ),
        }

    def pause_recovery(self, reason: str) -> None:
        self._recovery_paused_reason = " ".join(str(reason).split()) or "operator"

    def resume_recovery(self) -> None:
        self._recovery_paused_reason = None

    def reset_circuit(self, service_id: str) -> None:
        target = self._targets[service_id]
        target.circuit_open = False
        target.restart_attempts.clear()
        target.consecutive_failures = 0
        target.next_retry_at = 0.0
        target.last_action = "circuit_reset"

    async def check_once(self) -> dict[str, HealthStatus]:
        results: dict[str, HealthStatus] = {}
        for service_id, target in list(self._targets.items()):
            status = await self._run_check(target)
            results[service_id] = status
            target.last_status = status
            await self._handle_status(target, status)
        self._rounds += 1
        return results

    async def _run_check(self, target: _Target) -> HealthStatus:
        try:
            value = await asyncio.wait_for(
                target.check(), timeout=self.policy.check_timeout_s,
            )
            if isinstance(value, HealthStatus):
                return value
            return HealthStatus.healthy(target.service_id) if bool(value) else HealthStatus.unhealthy(
                target.service_id, "health probe returned false",
            )
        except asyncio.TimeoutError:
            return HealthStatus.unhealthy(target.service_id, "health check timeout")
        except Exception as exc:
            return HealthStatus.unhealthy(target.service_id, f"health check raised: {exc}")

    async def _handle_status(self, target: _Target, status: HealthStatus) -> None:
        if status.state is HealthState.HEALTHY:
            target.consecutive_failures = 0
            target.last_action = "healthy"
            return
        target.consecutive_failures += 1
        target.last_action = "unhealthy"
        self._record(target.service_id, "unhealthy")
        if target.consecutive_failures < self.policy.unhealthy_threshold:
            return
        if self._recovery_paused_reason is not None:
            target.last_action = "recovery_paused"
            self._record(target.service_id, "recovery_paused")
            return
        if self.policy.recovery_mode == "alert_only" or target.restart is None:
            target.last_action = "operator_alert"
            self._record(target.service_id, "operator_alert")
            return
        now = float(self._clock())
        self._prune_attempts(target, now)
        if target.circuit_open:
            target.last_action = "circuit_open"
            return
        if len(target.restart_attempts) >= self.policy.max_restart_attempts:
            target.circuit_open = True
            target.last_action = "circuit_open"
            self._record(target.service_id, "circuit_open")
            return
        if now < target.next_retry_at:
            target.last_action = "backoff"
            return
        target.restart_attempts.append(now)
        attempt = len(target.restart_attempts)
        target.next_retry_at = now + min(
            self.policy.backoff_initial_s * (2 ** (attempt - 1)),
            self.policy.backoff_max_s,
        )
        try:
            await asyncio.wait_for(target.restart(), timeout=self.policy.restart_timeout_s)
            target.restarts_total += 1
            target.consecutive_failures = 0
            target.last_action = "restarted"
            self._record(target.service_id, "restarted")
        except Exception as exc:
            target.restart_failures_total += 1
            target.last_action = "restart_failed"
            self._record(target.service_id, "restart_failed")
            self._log.warning(
                "service_restart_failed", service_id=target.service_id, error=str(exc),
            )

    def _prune_attempts(self, target: _Target, now: float) -> None:
        cutoff = now - self.policy.restart_window_s
        while target.restart_attempts and target.restart_attempts[0] < cutoff:
            target.restart_attempts.popleft()

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.check_once()
            except Exception as exc:
                self._log.warning("health_supervisor_round_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.policy.interval_s,
                )
            except asyncio.TimeoutError:
                continue

    def snapshot(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "running": self._running,
            "recovery_mode": self.policy.recovery_mode,
            "recovery_paused": self._recovery_paused_reason is not None,
            "recovery_paused_reason": self._recovery_paused_reason,
            "rounds": self._rounds,
            "targets": {
                service_id: {
                    "health": (
                        target.last_status.state.value if target.last_status else "unknown"
                    ),
                    "message": target.last_status.message if target.last_status else "",
                    "consecutive_failures": target.consecutive_failures,
                    "restart_attempts_in_window": len(target.restart_attempts),
                    "restarts_total": target.restarts_total,
                    "restart_failures_total": target.restart_failures_total,
                    "circuit_open": target.circuit_open,
                    "last_action": target.last_action,
                }
                for service_id, target in sorted(self._targets.items())
            },
        }

    def _record(self, service_id: str, action: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_health_supervisor_action"):
            self._metrics.record_health_supervisor_action(service_id, action)
