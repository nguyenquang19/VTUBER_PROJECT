from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.operations.metrics import MetricsCollector
from prometheus_client import CollectorRegistry
from services.state.agent import AgentState, AgentStateLimits, AgentStateReducer
from services.agent.agenda_policy import AgendaPolicy, AgendaPolicyConfig
from services.state.event_ledger import EventLedger
from services.agent.goal_manager import GoalLimits, GoalManager
from interfaces.state import GoalKind, GoalStatus
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _event(
    event_id: str, kind: AgentEventKind, payload: dict, *, seconds: int = 0,
) -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=kind,
        source=AgentEventSource.RUNTIME,
        timestamp=NOW + timedelta(seconds=seconds),
        confidence=1.0,
        payload=payload,
        provenance=EventProvenance("integration", source_event_id=event_id),
    )


def _stack(clock: Clock) -> tuple[AgentState, GoalManager]:
    metrics = MetricsCollector(registry=CollectorRegistry())
    state = AgentState(
        AgentStateReducer(AgentStateLimits(64, 3600, 8, 900, 320)),
        EventLedger(64, 3600, 3600, clock=clock, metrics=metrics), clock=clock,
    )
    policy = AgendaPolicy(AgendaPolicyConfig(
        priorities={
            GoalKind.ACK_DONATION: 100,
            GoalKind.WAIT_FOR_CHAT_ANSWER: 60,
            GoalKind.CONTINUE_THREAD: 40,
            GoalKind.ANSWER_FOLLOW_UP: 70,
            GoalKind.OPERATOR_PINNED: 90,
        },
        ttl_seconds={kind: 30 for kind in GoalKind},
    ), clock=clock)
    goals = GoalManager(
        GoalLimits(16, 8, 32, 240), clock=clock, metrics=metrics,
        on_active_changed=state.set_active_goal_ref, audit_sink=state.record,
        agenda_policy=policy,
    )
    state.add_event_listener(goals.handle_event)
    return state, goals


def _intention_id(goals: GoalManager) -> str:
    intention = goals.snapshot().current_intention
    assert intention is not None
    return intention.intention_id


def test_wait_donation_preemption_ack_and_resume_full_dod() -> None:
    clock = Clock()
    state, goals = _stack(clock)

    state.record(_event(
        "speech-question", AgentEventKind.SPEECH_FINAL,
        {"text": "Chat muốn tớ kể tiếp chuyện cà phê không?"},
    ))
    waiting = goals.snapshot().active
    assert waiting and waiting.kind is GoalKind.WAIT_FOR_CHAT_ANSWER
    assert state.snapshot().active_goal_ref == waiting.goal_id

    clock.now += timedelta(seconds=1)
    state.record(_event(
        "donation-1", AgentEventKind.DONATION_RECEIVED,
        {"text": "ủng hộ", "viewer_alias": "Lan", "amount_vnd": 100_000}, seconds=1,
    ))
    donation = goals.snapshot().active
    assert donation and donation.kind is GoalKind.ACK_DONATION
    assert [goal.goal_id for goal in goals.snapshot().suspended] == [waiting.goal_id]

    clock.now += timedelta(seconds=1)
    state.record(_event(
        "spoken-ack", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "ack_donation", "goal_id": donation.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=2,
    ))
    assert goals.snapshot().active.goal_id == waiting.goal_id  # type: ignore[union-attr]

    clock.now += timedelta(seconds=1)
    state.record(_event(
        "chat-answer", AgentEventKind.CHAT_RECEIVED,
        {"text": "Có, kể tiếp đi", "viewer_alias": "Minh"}, seconds=3,
    ))
    follow_up = goals.snapshot().active
    assert follow_up and follow_up.kind is GoalKind.ANSWER_FOLLOW_UP

    clock.now += timedelta(seconds=1)
    state.record(_event(
        "spoken-answer", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "read_chat", "goal_id": follow_up.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=4,
    ))
    snap = goals.snapshot()
    assert snap.active is None
    assert snap.candidates == ()
    assert snap.suspended == ()
    terminals = {goal.kind: goal.status for goal in snap.recent_terminal}
    assert terminals[GoalKind.ACK_DONATION] is GoalStatus.COMPLETED
    assert terminals[GoalKind.WAIT_FOR_CHAT_ANSWER] is GoalStatus.COMPLETED
    assert terminals[GoalKind.ANSWER_FOLLOW_UP] is GoalStatus.COMPLETED

    clock.now += timedelta(minutes=2)
    assert goals.snapshot().active is None
    assert state.snapshot().active_goal_ref is None


def test_listener_failure_does_not_reject_grounded_event() -> None:
    clock = Clock()
    state, _goals = _stack(clock)

    def broken(_event, _snapshot):
        raise RuntimeError("listener failed")

    state.add_event_listener(broken)
    assert state.record(_event("chat-safe", AgentEventKind.CHAT_RECEIVED, {"text": "xin chào"}))
    assert state.snapshot().recent_events[-1].event_id == "chat-safe"
    assert state.get_metrics()["agent_state_reduce_errors_total"] == 1


def test_continue_thread_ttl_refreshes_on_relevant_chat() -> None:
    clock = Clock()
    state, goals = _stack(clock)
    state.record(_event("question", AgentEventKind.CHAT_RECEIVED, {"text": "Cà phê nào ngon?"}))
    active = goals.snapshot().active
    assert active and active.kind is GoalKind.CONTINUE_THREAD
    original_expiry = active.expires_at
    clock.now += timedelta(seconds=10)
    state.record(_event("follow", AgentEventKind.CHAT_RECEIVED, {"text": "Còn loại nóng?"}, seconds=10))
    assert goals.snapshot().active.expires_at > original_expiry  # type: ignore[union-attr]


def test_director_goal_actions_complete_or_mark_progress_without_repeat() -> None:
    clock = Clock()
    state, goals = _stack(clock)
    state.record(_event(
        "speech-question", AgentEventKind.SPEECH_FINAL,
        {"text": "Chat wants another example?"},
    ))
    waiting = goals.snapshot().active
    assert waiting and waiting.kind is GoalKind.WAIT_FOR_CHAT_ANSWER

    state.record(_event(
        "asked-follow-up", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "ask_follow_up", "goal_id": waiting.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=1,
    ))
    marked = goals.snapshot().active
    assert marked and marked.goal_id == waiting.goal_id
    assert marked.metadata["follow_up_asked"] is True
    first_marker_event = marked.metadata["follow_up_asked_event_id"]

    state.record(_event(
        "asked-follow-up-again", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "ask_follow_up", "goal_id": waiting.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=2,
    ))
    assert goals.snapshot().active.metadata["follow_up_asked_event_id"] == first_marker_event  # type: ignore[union-attr]

    pinned = goals.pin_operator(reason="prepare grounded demo", success_condition="operator confirms done")
    assert pinned and goals.snapshot().active.goal_id == pinned.goal_id  # type: ignore[union-attr]
    state.record(_event(
        "shared-progress", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "share_goal_progress", "goal_id": pinned.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=3,
    ))
    active = goals.snapshot().active
    assert active and active.goal_id == pinned.goal_id
    assert active.metadata["progress_shared"] is True


def test_continue_thread_completes_only_on_matching_completed_speech() -> None:
    clock = Clock()
    state, goals = _stack(clock)
    state.record(_event(
        "chat-question", AgentEventKind.CHAT_RECEIVED,
        {"text": "Which grounded topic should continue?"},
    ))
    active = goals.snapshot().active
    assert active and active.kind is GoalKind.CONTINUE_THREAD
    state.record(_event(
        "wrong-action", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "self_talk", "goal_id": active.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=1,
    ))
    assert goals.snapshot().active.goal_id == active.goal_id  # type: ignore[union-attr]
    state.record(_event(
        "continued", AgentEventKind.SPEECH_COMPLETED,
        {
            "action": "continue_thread", "goal_id": active.goal_id,
            "intention_id": _intention_id(goals),
        }, seconds=2,
    ))
    assert goals.snapshot().active is None
    assert any(
        item.goal_id == active.goal_id and item.status is GoalStatus.COMPLETED
        for item in goals.snapshot().recent_terminal
    )
