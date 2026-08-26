from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.agent.agenda_policy import AgendaPolicyConfig
from services.agent.goal_manager import GoalLimits, GoalManager
from interfaces.state import (
    Goal,
    GoalKind,
    GoalSource,
    GoalStatus,
    ShortIntentionStatus,
)
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    ConversationMove,
    EventProvenance,
    GroundedEvent,
    OpenThread,
    ThreadStatus,
)

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
    parent_thread_id: str | None = None,
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
        parent_thread_id=parent_thread_id,
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
    intention_status = {item.goal_id: item.status for item in snap.intentions}
    assert intention_status == {
        "donation": ShortIntentionStatus.ACTIVE,
        "thread": ShortIntentionStatus.SUSPENDED,
    }
    assert manager.complete("donation")
    assert manager.snapshot().active.goal_id == "thread"  # type: ignore[union-attr]
    assert manager.snapshot().current_intention.status is ShortIntentionStatus.ACTIVE  # type: ignore[union-attr]
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


def test_reconcile_threads_cancels_stale_goal_and_activates_valid_next() -> None:
    manager = _manager(Clock())
    assert manager.submit(_goal("stale", parent_thread_id="thread-old"))
    assert manager.submit(_goal(
        "valid", priority=30, parent_thread_id="thread-live",
    ))

    assert manager.reconcile_threads({"thread-live"}) == 1

    snapshot = manager.snapshot()
    assert snapshot.active is not None
    assert snapshot.active.goal_id == "valid"
    stale = next(goal for goal in snapshot.recent_terminal if goal.goal_id == "stale")
    assert stale.status is GoalStatus.CANCELLED
    assert stale.suspend_reason == "parent_thread_missing"
    assert manager.get_metrics()["goal_reconciled_total"] == 1


def test_reconcile_threads_keeps_unbound_and_operator_goals() -> None:
    manager = _manager(Clock())
    assert manager.submit(_goal("unbound"))
    assert manager.submit(_goal(
        "operator", GoalKind.OPERATOR_PINNED, 90,
        parent_thread_id="thread-old",
    ))

    assert manager.reconcile_threads(set()) == 0
    snapshot = manager.snapshot()
    assert snapshot.active is not None
    assert snapshot.active.goal_id == "operator"
    assert any(goal.goal_id == "unbound" for goal in snapshot.suspended)


def test_delivered_chat_focus_cancels_other_thread_and_unlocks_selected() -> None:
    manager = _manager(Clock(), candidates_max=8)
    assert manager.submit(_goal(
        "old", parent_thread_id="thread-old",
        metadata={"source_event_id": "agent:chat:old", "source_delivered": False},
    ))
    assert manager.submit(_goal(
        "selected", parent_thread_id="thread-selected",
        metadata={"source_event_id": "agent:chat:selected", "source_delivered": False},
    ))

    changed = manager.focus_delivered_thread(
        "thread-selected", source_event_ids={"agent:chat:selected"},
    )

    snapshot = manager.snapshot()
    assert changed == 2
    assert snapshot.active is not None
    assert snapshot.active.goal_id == "selected"
    assert snapshot.active.metadata["source_delivered"] is True
    assert any(
        goal.goal_id == "old" and goal.status is GoalStatus.CANCELLED
        for goal in snapshot.recent_terminal
    )


def test_delivered_move_atomically_keeps_same_parent_until_park() -> None:
    manager = _manager(Clock(), candidates_max=8)
    assert manager.submit(_goal(
        "active", parent_thread_id="thread-a",
        metadata={"source_delivered": True},
    ))
    assert manager.submit(_goal("duplicate", parent_thread_id="thread-a"))
    assert manager.submit(_goal("other", parent_thread_id="thread-b"))
    thread = OpenThread(
        "thread-a", "topic A", "summary A", NOW, NOW,
        NOW + timedelta(minutes=5), status=ThreadStatus.ACTIVE,
        last_move=ConversationMove.SUMMARIZE,
        next_move=ConversationMove.PARK,
    )
    event = GroundedEvent(
        event_id="speech-summary",
        kind=AgentEventKind.SPEECH_COMPLETED,
        source=AgentEventSource.DIRECTOR,
        timestamp=NOW,
        confidence=1.0,
        payload={
            "action": "continue_thread",
            "goal_id": "active",
            "intention_id": manager.snapshot().current_intention.intention_id,
            "conversation_move": "summarize",
        },
        provenance=EventProvenance("test"),
    )

    manager.handle_event(event, AgentStateSnapshot(open_threads=(thread,)))

    snapshot = manager.snapshot()
    assert snapshot.active is not None
    assert snapshot.active.parent_thread_id == "thread-a"
    assert snapshot.active.metadata["boundary_successor"] is True
    assert snapshot.active.metadata["source_delivered"] is True
    assert any(goal.goal_id == "duplicate" for goal in snapshot.recent_terminal)
    assert any(goal.goal_id == "other" for goal in snapshot.candidates)


def test_delivered_park_releases_boundary_and_room_clear_removes_old_goals() -> None:
    manager = _manager(Clock(), candidates_max=8)
    assert manager.submit(_goal(
        "park", parent_thread_id="thread-a",
        metadata={"source_delivered": True},
    ))
    assert manager.submit(_goal("other", parent_thread_id="thread-b"))
    parked = OpenThread(
        "thread-a", "topic A", "closed A", NOW, NOW,
        NOW + timedelta(minutes=5), status=ThreadStatus.PARKED,
        last_move=ConversationMove.PARK,
        next_move=ConversationMove.RESUME,
    )
    event = GroundedEvent(
        event_id="speech-park",
        kind=AgentEventKind.SPEECH_COMPLETED,
        source=AgentEventSource.DIRECTOR,
        timestamp=NOW,
        confidence=1.0,
        payload={
            "action": "continue_thread",
            "goal_id": "park",
            "intention_id": manager.snapshot().current_intention.intention_id,
            "conversation_move": "park",
        },
        provenance=EventProvenance("test"),
    )

    manager.handle_event(event, AgentStateSnapshot(open_threads=(parked,)))
    assert manager.snapshot().active is not None
    assert manager.snapshot().active.goal_id == "other"
    assert manager.clear_continue_threads(reason="room_reaction_delivered") == 1
    assert manager.snapshot().active is None


def test_short_intention_advances_three_linear_steps_then_completes_goal() -> None:
    manager = _manager(Clock())
    goal = replace(_goal("multi"), steps=("one", "two", "three"))
    assert manager.submit(goal)

    for index in range(3):
        snapshot = manager.snapshot()
        intention = snapshot.current_intention
        assert intention is not None
        assert intention.step_index == index
        assert intention.status is ShortIntentionStatus.ACTIVE
        assert manager.record_action_outcome(
            goal.goal_id,
            intention.intention_id,
            f"outcome-{index}",
            outcome="succeeded",
            reason="verified_delivery",
        )

    snapshot = manager.snapshot()
    assert snapshot.active is None
    assert snapshot.current_intention is None
    assert [item.status for item in snapshot.recent_intentions] == [
        ShortIntentionStatus.COMPLETED,
        ShortIntentionStatus.COMPLETED,
        ShortIntentionStatus.COMPLETED,
    ]


def test_action_outcome_rejects_stale_and_duplicate_intention_identity() -> None:
    manager = _manager(Clock())
    assert manager.submit(replace(_goal("multi"), steps=("one", "two")))
    first = manager.snapshot().current_intention
    assert first is not None
    assert manager.record_action_outcome(
        "multi", first.intention_id, "delivery-1",
        outcome="succeeded", reason="verified",
    )
    second = manager.snapshot().current_intention
    assert second is not None
    assert second.intention_id != first.intention_id
    assert not manager.record_action_outcome(
        "multi", first.intention_id, "delivery-1",
        outcome="succeeded", reason="duplicate",
    )
    assert manager.snapshot().current_intention == second


def test_action_failure_and_ttl_cancel_intentions_deterministically() -> None:
    clock = Clock()
    manager = _manager(clock)
    assert manager.submit(_goal("failed"))
    intention = manager.snapshot().current_intention
    assert intention is not None
    assert manager.record_action_outcome(
        "failed", intention.intention_id, "attempt-1",
        outcome="failed", reason="not_delivered",
    )
    assert manager.snapshot().recent_intentions[-1].status is ShortIntentionStatus.FAILED

    assert manager.submit(_goal("expires", ttl=1))
    clock.now += timedelta(seconds=2)
    assert manager.snapshot().active is None
    assert manager.snapshot().recent_intentions[-1].status is ShortIntentionStatus.CANCELLED
    assert manager.snapshot().recent_intentions[-1].reason_code == "ttl_expired"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("candidates_max", True),
        ("suspended_max", "8"),
        ("terminal_history_max", 0),
        ("metadata_text_max_chars", 2.5),
        ("short_intention_max_steps", 4),
        ("operator_priority", -1),
        ("action_failure_policy", "retry"),
        ("action_cancellation_policy", "ignore"),
    ],
)
def test_goal_limits_reject_coercion_and_invalid_policy(
    field_name: str, value: object,
) -> None:
    values: dict[str, object] = {
        "candidates_max": 8,
        "suspended_max": 4,
        "terminal_history_max": 16,
        "metadata_text_max_chars": 240,
        "short_intention_max_steps": 3,
        "operator_priority": 90,
        "operator_ttl_s": 3600,
        "action_failure_policy": "fail",
        "action_cancellation_policy": "cancel",
    }
    values[field_name] = value
    with pytest.raises(ValueError):
        GoalLimits(**values)  # type: ignore[arg-type]


def test_goal_contract_rejects_naive_time_coerced_fields_and_unsupported_metadata() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(
            _goal("naive"),
            created_at=NOW.replace(tzinfo=None),
            expires_at=(NOW + timedelta(minutes=1)).replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="priority"):
        replace(_goal("bool-priority"), priority=True)
    with pytest.raises(ValueError, match="one to three"):
        replace(_goal("too-many"), steps=("1", "2", "3", "4"))
    with pytest.raises(ValueError, match="unsupported"):
        replace(_goal("bad-metadata"), metadata={"object": object()})


def test_goal_yaml_loads_strict_phase_11_policies() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    limits = GoalLimits.from_loader(loader)
    agenda = AgendaPolicyConfig.from_loader(loader)
    assert limits.short_intention_max_steps == 3
    assert limits.action_failure_policy == "fail"
    assert limits.action_cancellation_policy == "cancel"
    assert set(agenda.priorities) == set(GoalKind)
    assert set(agenda.ttl_seconds) == set(GoalKind)


def test_short_intention_replay_is_deterministic_for_same_events_and_outcomes() -> None:
    def replay() -> dict[str, object]:
        manager = _manager(Clock())
        manager.submit(replace(_goal("base"), steps=("one", "two")))
        first = manager.snapshot().current_intention
        assert first is not None
        manager.record_action_outcome(
            "base", first.intention_id, "delivery-1",
            outcome="succeeded", reason="verified",
        )
        second = manager.snapshot().current_intention
        assert second is not None
        manager.record_action_outcome(
            "base", second.intention_id, "delivery-2",
            outcome="failed", reason="not_delivered",
        )
        return manager.snapshot().to_dict()

    assert replay() == replay()
