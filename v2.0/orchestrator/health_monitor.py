"""Periodic, failure-isolated health checks for registered services.

Service checks are polled on a bounded interval; state changes are published to
the event bus and log. Backend-specific recovery remains outside this generic monitor.

Interval comes from config. A failed or timed-out check becomes UNHEALTHY without
stopping the loop or other service checks.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable

from interfaces.base import HealthState, HealthStatus
from orchestrator.logger import get_logger

#: 1 check = coroutine trả HealthStatus (thường là Service.health_check)
HealthCheckFn = Callable[[], Awaitable[HealthStatus]]


class HealthMonitor:
    def __init__(
        self,
        interval_s: float = 10.0,
        check_timeout_s: float = 5.0,
        event_bus: Any = None,
    ) -> None:
        self.interval_s = interval_s
        self.check_timeout_s = check_timeout_s
        self._event_bus = event_bus
        self._log = get_logger("health_monitor")
        self._checks: dict[str, HealthCheckFn] = {}
        self._last: dict[str, HealthStatus] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._rounds = 0

    @classmethod
    def from_config(cls, loader, event_bus: Any = None) -> HealthMonitor:
        return cls(
            interval_s=float(loader.get("system", "health.interval_s", 10.0)),
            check_timeout_s=float(loader.get("system", "health.check_timeout_s", 5.0)),
            event_bus=event_bus,
        )

    def register(self, service_id: str, check: HealthCheckFn) -> None:
        self._checks[service_id] = check

    def register_service(self, service: Any) -> None:
        """Đăng ký object có .service_id + .health_check()."""
        self.register(service.service_id, service.health_check)

    def unregister(self, service_id: str) -> None:
        self._checks.pop(service_id, None)
        self._last.pop(service_id, None)

    async def check_once(self) -> dict[str, HealthStatus]:
        """Chạy 1 vòng health check tất cả service. Trả snapshot."""
        results: dict[str, HealthStatus] = {}
        for service_id, check in list(self._checks.items()):
            status = await self._run_one(service_id, check)
            results[service_id] = status
            self._emit_if_changed(service_id, status)
            self._last[service_id] = status
        self._rounds += 1
        return results

    async def _run_one(self, service_id: str, check: HealthCheckFn) -> HealthStatus:
        try:
            return await asyncio.wait_for(check(), timeout=self.check_timeout_s)
        except asyncio.TimeoutError:
            return HealthStatus.unhealthy(service_id, "health check timeout")
        except Exception as e:
            return HealthStatus.unhealthy(service_id, f"health check raised: {e}")

    def _emit_if_changed(self, service_id: str, status: HealthStatus) -> None:
        prev = self._last.get(service_id)
        if prev is not None and prev.state == status.state:
            return  # chỉ log/emit khi trạng thái đổi (tránh spam)
        level = self._log.info if status.state is HealthState.HEALTHY else self._log.warning
        level(
            "health_status_changed",
            service_id=service_id,
            state=status.state.value,
            message=status.message,
            previous=prev.state.value if prev else None,
        )
        if self._event_bus is not None:
            self._event_bus.publish(
                "health_status",
                {"service_id": service_id, "state": status.state.value, "message": status.message},
            )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            sid: {"state": st.state.value, "message": st.message}
            for sid, st in self._last.items()
        }

    def is_all_healthy(self) -> bool:
        return all(st.state is HealthState.HEALTHY for st in self._last.values())

    @property
    def rounds_completed(self) -> int:
        return self._rounds

    async def _loop(self) -> None:
        # Dừng bằng Event, KHÔNG dựa vào task.cancel(): asyncio.wait_for trong
        # _run_one có thể nuốt CancelledError khi check hoàn tất đúng lúc cancel
        # (bug asyncio đã biết) → cancel không đáng tin trong loop poll mật độ cao.
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_s)
                break  # event set trong lúc chờ → dừng
            except asyncio.TimeoutError:
                pass  # hết interval → tới lượt check
            try:
                await self.check_once()
            except Exception as e:
                self._log.warning("health_loop_check_error", error=str(e))

    def start(self) -> None:
        if self._task is None:
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            if self._stop_event is not None:
                self._stop_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self.interval_s + 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            self._task = None
            self._stop_event = None
