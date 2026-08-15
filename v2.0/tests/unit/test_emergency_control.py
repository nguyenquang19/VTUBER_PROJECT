from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.operations.emergency_control import EmergencyController
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_types import Goal, GoalKind, GoalSource, GoalStatus


@pytest.mark.asyncio
async def test_trigger_closes_gates_before_cancelling_and_resume_prunes_first() -> None:
    events: list[str] = []
    controller: EmergencyController

    async def pause() -> None:
        events.append(f"pause:{controller.permits_speech()}")

    async def cancel_speech() -> None:
        events.append(f"speech:{controller.permits_speech()}")

    async def cancel_environment() -> None:
        events.append(f"environment:{controller.permits_environment_action()}")

    async def prune() -> None:
        events.append(f"prune:{controller.permits_speech()}")

    async def resume() -> None:
        events.append(f"resume:{controller.permits_speech()}")

    controller = EmergencyController(
        pause_actions=pause, resume_actions=resume,
        cancel_speech=cancel_speech,
        cancel_environment_actions=cancel_environment,
        prune_stale_work=prune,
    )
    await controller.start()

    assert await controller.trigger("operator") is True
    assert controller.snapshot()["latched"] is True
    assert set(events) == {"pause:False", "speech:False", "environment:False"}
    assert await controller.resume("reviewed") is True
    assert events[-2:] == ["prune:False", "resume:False"]
    assert controller.permits_speech() is True


@pytest.mark.asyncio
async def test_trigger_is_idempotent_and_degraded_failure_stays_latched() -> None:
    calls: dict[str, int] = {"pause": 0}

    async def pause() -> None:
        calls["pause"] += 1

    async def fail_cancel() -> None:
        raise RuntimeError("device unavailable")

    controller = EmergencyController(
        pause_actions=pause, resume_actions=pause, cancel_speech=fail_cancel,
    )
    await controller.start()

    assert await controller.trigger() is False
    assert await controller.trigger() is True
    assert calls["pause"] == 1
    assert controller.permits_speech() is False


@pytest.mark.asyncio
async def test_audit_metrics_and_recovery_are_coordinated() -> None:
    audit: list[tuple[str, str, str]] = []
    recovery: list[str] = []

    async def noop() -> None:
        return None

    class Metrics:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str]] = []

        def record_emergency_control(self, action: str, outcome: str) -> None:
            self.rows.append((action, outcome))

    metrics: Any = Metrics()
    controller = EmergencyController(
        pause_actions=noop, resume_actions=noop,
        pause_recovery=recovery.append,
        resume_recovery=lambda: recovery.append("resumed"),
        audit=lambda action, target, outcome: audit.append((action, target, outcome)),
        metrics=metrics,
    )
    await controller.start()
    await controller.trigger()
    await controller.resume()

    assert recovery == ["emergency_stop", "resumed"]
    assert [row[0] for row in audit] == ["emergency_stop", "emergency_resume"]
    assert metrics.rows == [
        ("emergency_stop", "completed"), ("emergency_resume", "completed"),
    ]


@pytest.mark.asyncio
async def test_resume_prunes_expired_goal_instead_of_resurrecting_it() -> None:
    now = [datetime(2026, 8, 9, tzinfo=timezone.utc)]
    manager = GoalManager(GoalLimits(3, 2, 4, 80), clock=lambda: now[0])
    manager.submit(Goal(
        goal_id="stale", kind=GoalKind.CONTINUE_THREAD, status=GoalStatus.CANDIDATE,
        priority=40, reason="old context", source=GoalSource.RULE,
        created_at=now[0], expires_at=now[0] + timedelta(seconds=1),
        success_conditions=("done",),
    ))

    async def noop() -> None:
        return None

    async def prune() -> None:
        manager.snapshot()

    controller = EmergencyController(
        pause_actions=noop, resume_actions=noop, prune_stale_work=prune,
    )
    await controller.start()
    await controller.trigger()
    now[0] += timedelta(seconds=2)
    assert await controller.resume() is True

    snapshot = manager.snapshot()
    assert snapshot.active is None
    assert snapshot.recent_terminal[0].status is GoalStatus.EXPIRED
