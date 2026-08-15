from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.metrics_collector import MetricsCollector
from services.agent.open_thread_manager import OpenThreadLimits, OpenThreadManager
from services.agent.types import ThreadEvidence, ThreadKind

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _manager(clock: Clock, metrics=None, **over: int) -> OpenThreadManager:
    return OpenThreadManager(OpenThreadLimits(
        max_open=over.get("max_open", 2),
        ttl_seconds=over.get("ttl_seconds", 30),
        evidence_max=over.get("evidence_max", 2),
        field_max_chars=over.get("field_max_chars", 80),
        terminal_history_max=over.get("terminal_history_max", 4),
    ), clock=clock, metrics=metrics)


def _evidence(event_id: str, excerpt: str = "Mai kể tiếp chuyện này nhé") -> ThreadEvidence:
    return ThreadEvidence(event_id, excerpt, "rule", 1.0)


def test_create_update_resolve_with_human_readable_evidence() -> None:
    clock = Clock()
    manager = _manager(clock)
    thread = manager.create(
        kind=ThreadKind.STORY,
        topic="chuyện cà phê",
        summary="Mai đang kể chuyện cà phê",
        evidence=_evidence("speech-1"),
    )
    assert thread is not None
    assert thread.evidence[0].excerpt == "Mai kể tiếp chuyện này nhé"
    clock.now += timedelta(seconds=5)
    assert manager.update(
        thread.thread_id,
        summary="đã kể đến quán đầu tiên",
        evidence=_evidence("speech-2", "quán đầu tiên khá đông"),
    )
    updated = manager.snapshot()[0]
    assert updated.summary == "đã kể đến quán đầu tiên"
    assert [item.source_event_id for item in updated.evidence] == ["speech-1", "speech-2"]
    assert manager.resolve(thread.thread_id, reason="story completed")
    assert manager.snapshot() == ()
    assert manager.recent_terminal()[-1][1] == "story completed"


def test_duplicate_evidence_capacity_and_expiry_are_deterministic() -> None:
    clock = Clock()
    manager = _manager(clock, max_open=1, ttl_seconds=10)
    first = manager.create(
        kind=ThreadKind.QUESTION, topic="one", summary="one?", evidence=_evidence("e1"),
    )
    assert first is not None
    assert manager.create(
        kind=ThreadKind.QUESTION, topic="duplicate", summary="duplicate?",
        evidence=_evidence("e1"),
    ) is None
    second = manager.create(
        kind=ThreadKind.PROMISE, topic="two", summary="will return",
        evidence=_evidence("e2"),
    )
    assert second is not None
    assert manager.recent_terminal()[-1][1] == "capacity"
    clock.now += timedelta(seconds=11)
    assert manager.expire() == 1
    assert manager.snapshot() == ()


def test_promise_completion_and_thread_metrics_are_observable() -> None:
    clock = Clock()
    metrics = MetricsCollector()
    manager = _manager(clock, metrics=metrics)
    thread = manager.create(
        kind=ThreadKind.PROMISE, topic="promise", summary="Mai will return",
        evidence=_evidence("e1"),
    )
    assert thread and manager.resolve(thread.thread_id, reason="done")
    snapshot = metrics.thread_snapshot()
    assert snapshot["opened:promise"] == 1
    assert snapshot["resolved:promise"] == 1
    assert snapshot["promise_completed:promise"] == 1


async def test_service_lifecycle() -> None:
    manager = _manager(Clock())
    assert not (await manager.health_check()).is_ok
    await manager.start()
    assert (await manager.health_check()).is_ok
    await manager.stop()
    assert not (await manager.health_check()).is_ok


def test_agent_state_uses_manager_threads_before_notifying_listeners() -> None:
    from services.agent.agent_state import AgentState, AgentStateLimits, AgentStateReducer
    from services.agent.event_ledger import EventLedger
    from services.agent.thread_detector import RuleThreadDetector
    from services.agent.types import (
        AgentEventKind, AgentEventSource, EventProvenance, GroundedEvent,
    )

    clock = Clock()
    manager = _manager(clock)
    manager.set_detector(RuleThreadDetector())
    state = AgentState(
        AgentStateReducer(AgentStateLimits(8, 60, 2, 30, 100)),
        EventLedger(8, 60, 60, clock=clock), clock=clock, thread_manager=manager,
    )
    seen = []
    state.add_event_listener(lambda _event, snapshot: seen.append(snapshot.open_threads))
    state.record(GroundedEvent(
        "q1", AgentEventKind.CHAT_RECEIVED, AgentEventSource.CHAT, NOW, 1.0,
        {"text": "Mai thích món nào?"}, EventProvenance("test", "q1"),
    ))
    assert state.snapshot().open_threads[0].origin_event_id == "q1"
    assert seen[0][0].evidence[0].excerpt == "Mai thích món nào?"
