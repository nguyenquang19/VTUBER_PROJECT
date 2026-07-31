"""Test deadlock watchdog 7.10.4 (Phase 2, 2.B)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.state_machine import ConversationStateMachine
from orchestrator.state_watchdog import StateWatchdog

REPO_ROOT = Path(__file__).resolve().parents[2]

MAXES = {"IDLE": None, "THINKING": 10, "SPEAKING": 30, "COOLDOWN": 5, "PAUSED": None}


def sm() -> ConversationStateMachine:
    return ConversationStateMachine(auto_cooldown=False)


def wd(machine, auto_recover: bool = True, **kw) -> StateWatchdog:
    return StateWatchdog(machine, max_time_in_state=MAXES, check_interval_s=0.05,
                         auto_recover=auto_recover, **kw)


def _age(machine, seconds: float) -> None:
    """Giả lập đã ở state này `seconds` giây."""
    machine.state_entered_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)


class TestDetection:
    async def test_stuck_thinking_detected(self) -> None:
        m = sm()
        await m.trigger_received()          # IDLE → THINKING
        _age(m, 60)                         # quá ngưỡng 10s
        w = wd(m)
        handled = await w.check_once()
        assert handled is True
        assert w.get_metrics()["watchdog_deadlocks_total"] == 1
        assert w.get_metrics()["watchdog_last_deadlock_state"] == "THINKING"

    async def test_under_threshold_no_deadlock(self) -> None:
        m = sm()
        await m.trigger_received()
        _age(m, 2)                          # < 10s
        w = wd(m)
        assert await w.check_once() is False
        assert m.state == "THINKING"

    async def test_idle_never_deadlock(self) -> None:
        m = sm()
        _age(m, 99999)                      # IDLE lâu vô hạn vẫn OK
        w = wd(m)
        assert await w.check_once() is False

    async def test_paused_never_deadlock(self) -> None:
        m = sm()
        await m.emergency_stop()            # → PAUSED
        _age(m, 99999)
        w = wd(m)
        assert await w.check_once() is False
        assert m.state == "PAUSED"


class TestRecovery:
    async def test_auto_recover_returns_idle(self) -> None:
        m = sm()
        await m.trigger_received()
        _age(m, 60)
        w = wd(m, auto_recover=True)
        await w.check_once()
        assert m.state == "IDLE"

    async def test_no_auto_recover_stays_paused(self) -> None:
        m = sm()
        await m.trigger_received()
        _age(m, 60)
        w = wd(m, auto_recover=False)
        await w.check_once()
        assert m.state == "PAUSED"


class TestLoopLifecycle:
    async def test_loop_detects_and_stops_clean(self) -> None:
        m = sm()
        await m.trigger_received()
        _age(m, 60)
        w = wd(m)
        w.start()
        await asyncio.sleep(0.2)            # đủ vài vòng poll
        await asyncio.wait_for(w.stop(), timeout=2.0)   # KHÔNG hang
        assert w.get_metrics()["watchdog_deadlocks_total"] >= 1

    async def test_stop_without_deadlock_clean(self) -> None:
        m = sm()                            # ở IDLE, không deadlock
        w = wd(m)
        w.start()
        await asyncio.sleep(0.12)
        await asyncio.wait_for(w.stop(), timeout=2.0)
        assert w.get_metrics()["watchdog_deadlocks_total"] == 0


class TestFromConfig:
    def test_reads_thresholds(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        w = StateWatchdog.from_config(loader, sm())
        assert w._max["THINKING"] == 10.0
        assert w._max["SPEAKING"] == 30.0
        assert w._max["COOLDOWN"] == 5.0
        assert "IDLE" not in w._max      # null → không giám sát
        assert "PAUSED" not in w._max
        assert w.check_interval_s == 1.0
