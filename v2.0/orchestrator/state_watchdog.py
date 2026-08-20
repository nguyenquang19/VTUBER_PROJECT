"""Deadlock watchdog for bounded conversation-state recovery.

Poll định kỳ; nếu state ở quá `max_time_in_state` (config state_machine.yaml
auto_recovery) → coi là deadlock → emergency_stop + (tuỳ chọn) recover về IDLE.

IDLE/PAUSED có ngưỡng null → không bao giờ coi là deadlock (ở lâu là bình thường).

Dừng bằng `asyncio.Event` (KHÔNG task.cancel) — cùng lý do như HealthMonitor:
asyncio.wait_for có thể nuốt CancelledError → cancel không đáng tin trong loop poll.
Không gọi thẳng service — chỉ thao tác state machine qua API công khai (N8).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from orchestrator.logger import get_logger


class StateWatchdog:
    def __init__(
        self,
        state_machine: Any,
        max_time_in_state: dict[str, float | None],
        check_interval_s: float = 1.0,
        auto_recover: bool = True,
        event_bus: Any = None,
    ) -> None:
        self._sm = state_machine
        # bỏ ngưỡng None (không giám sát state đó)
        self._max: dict[str, float] = {
            k: float(v) for k, v in (max_time_in_state or {}).items() if v is not None
        }
        self.check_interval_s = check_interval_s
        self.auto_recover = auto_recover
        self._event_bus = event_bus
        self._log = get_logger("state_watchdog")

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._deadlocks_total = 0
        self._last_deadlock_state: str | None = None

    @classmethod
    def from_config(cls, loader, state_machine: Any, event_bus: Any = None) -> "StateWatchdog":
        ar = loader.get("state_machine", "state_machine.auto_recovery", {}) or {}
        return cls(
            state_machine,
            max_time_in_state=ar.get("max_time_in_state_seconds", {}) or {},
            check_interval_s=float(ar.get("stuck_state_check_interval_seconds", 1)),
            event_bus=event_bus,
        )

    # ---------- detection ----------

    async def check_once(self) -> bool:
        """1 lần kiểm tra. Trả True nếu vừa xử lý 1 deadlock."""
        state = self._sm.state
        max_s = self._max.get(state)
        if max_s is None:
            return False  # state không giám sát (IDLE/PAUSED hoặc không cấu hình)
        elapsed_s = self._sm.time_in_state_ms() / 1000.0
        if elapsed_s <= max_s:
            return False
        await self._handle_deadlock(state, elapsed_s, max_s)
        return True

    async def _handle_deadlock(self, state: str, elapsed_s: float, max_s: float) -> None:
        self._deadlocks_total += 1
        self._last_deadlock_state = state
        self._log.error(
            "state_deadlock",
            state=state,
            elapsed_s=round(elapsed_s, 2),
            max_s=max_s,
            auto_recover=self.auto_recover,
        )
        if self._event_bus is not None:
            self._event_bus.publish(
                "state_deadlock", {"state": state, "elapsed_s": round(elapsed_s, 2)}
            )
        try:
            await self._sm.emergency_stop()
            if self.auto_recover:
                await self._sm.resume()  # PAUSED → IDLE
        except Exception as e:  # recovery lỗi không được giết watchdog (N7)
            self._log.error("state_recovery_failed", state=state, error=str(e))

    # ---------- loop ----------

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.check_interval_s)
                break  # event set → dừng
            except asyncio.TimeoutError:
                pass  # hết interval → tới lượt check
            try:
                await self.check_once()
            except Exception as e:
                self._log.warning("watchdog_check_error", error=str(e))

    def start(self) -> None:
        if self._task is None:
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            if self._stop_event is not None:
                self._stop_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self.check_interval_s + 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            self._task = None
            self._stop_event = None

    # ---------- inspection ----------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "watchdog_deadlocks_total": self._deadlocks_total,
            "watchdog_last_deadlock_state": self._last_deadlock_state,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "deadlocks_total": self._deadlocks_total,
            "last_deadlock_state": self._last_deadlock_state,
            "watched_states": sorted(self._max.keys()),
        }
