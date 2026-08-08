from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_types import Goal, GoalKind, GoalSource, GoalStatus

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _goal(
    goal_id: str,
    kind: GoalKind = GoalKind.CONTINUE_THREAD,
    priority: int = 40,
    *,
    ttl: int = 60,
    metadata: dict | None = None,
) -> Goal:
    return Goal(
        goal_id=goal_id,
        kind=kind,
        status=GoalStatus.CANDIDATE,
        priority=priority,
        reason=f"grounded {goal_id}",
        source=GoalSource.RULE,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl),
        success_conditions=("done",),
        metadata=metadata or {},
    )


def _manager(clock: Clock, **limits: int) -> GoalManager:
    return GoalManager(GoalLimits(
        candidates_max=limits.get("candidates_max", 3),
        suspended_max=limits.get("suspended_max", 2),
        terminal_history_max=limits.get("terminal_history_max", 4),
        metadata_text_max_chars=limits.get("metadata_text_max_chars", 20),
    ), clock=clock)


def test_first_goal_activates_and_snapshot_is_immutable() -> None:
    manager = _manager(Clock())
    assert manager.submit(_goal("g1"))
    snap = manager.snapshot()
    assert snap.active is not None
    assert snap.active.status is GoalStatus.ACTIVE
    assert snap.active.goal_id == "g1"
    exported = snap.to_dict()
    exported["active"]["metadata"]["x"] = "fake"
    assert "x" not in snap.active.metadata


def test_higher_priority_preempts_then_completed_resumes_previous() -> None:
    active_refs: list[str | None] = []
    clock = Clock()
    manager = GoalManager(
        GoalLimits(3, 2, 4, 40), clock=clock, on_active_changed=active_refs.append,
    )
    manager.submit(_goal("thread", priority=40))
    manager.submit(_goal("donation", GoalKind.ACK_DONATION, 100))
    snap = manager.snapshot()
    assert snap.active and snap.active.goal_id == "donation"
    assert [g.goal_id for g in snap.suspended] == ["thread"]
    assert manager.complete("donation")
    assert manager.snapshot().active.goal_id == "thread"  # type: ignore[union-attr]
    assert active_refs == ["thread", "donation", "thread"]


def test_expired_or_irrelevant_suspended_goal_does_not_resume() -> None:
    clock = Clock()
    manager = _manager(clock)
    manager.submit(_goal("old", priority=40, metadata={"relevant": False}))
    manager.submit(_goal("donation", GoalKind.ACK_DONATION, 100))
    manager.complete("donation")
    assert manager.snapshot().active is None
    assert any(
        goal.goal_id == "old" and goal.status is GoalStatus.EXPIRED
        for goal in manager.snapshot().recent_terminal
    )


def test_ttl_prunes_active_candidate_and_suspended() -> None:
    clock = Clock()
    manager = _manager(clock)
    manager.submit(_goal("active", ttl=5))
    manager.submit(_goal("candidate", priority=20, ttl=5))
    manager.submit(_goal("preempt", priority=80, ttl=5))
    clock.now += timedelta(seconds=6)
    snap = manager.snapshot()
    assert snap.active is None
    assert snap.candidates == ()
    assert snap.suspended == ()
    assert {goal.status for goal in snap.recent_terminal} == {GoalStatus.EXPIRED}


def test_candidate_cap_keeps_highest_and_rejects_duplicate() -> None:
    manager = _manager(Clock(), candidates_max=2)
    manager.submit(_goal("active", priority=100))
    assert manager.submit(_goal("low", priority=10))
    assert manager.submit(_goal("mid", priority=20))
    assert not manager.submit(_goal("lower", priority=1))
    assert not manager.submit(_goal("mid", priority=20))
    assert [g.goal_id for g in manager.snapshot().candidates] == ["mid", "low"]


def test_cancel_unknown_is_safe_and_metadata_is_bounded() -> None:
    manager = _manager(Clock(), metadata_text_max_chars=8)
    assert not manager.cancel("missing", reason="operator")
    manager.submit(_goal("g1", metadata={"evidence": "abcdefghijkl"}))
    assert len(str(manager.snapshot().active.metadata["evidence"])) <= 8  # type: ignore[union-attr]


async def test_service_lifecycle_and_metrics() -> None:
    manager = _manager(Clock())
    assert not (await manager.health_check()).is_ok
    await manager.start()
    assert (await manager.health_check()).is_ok
    manager.submit(_goal("g1"))
    assert manager.get_metrics()["goal_created_total"] == 1
    await manager.stop()
