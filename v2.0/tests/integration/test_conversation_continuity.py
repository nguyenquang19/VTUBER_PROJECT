from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.operations.metrics import MetricsCollector
from services.cognition.compatibility_context import (
    ConversationContextComposer, ConversationContextConfig,
)
from services.agent.open_thread_manager import OpenThreadLimits, OpenThreadManager
from services.agent.repair_policy import ConversationRepairPolicy, RepairPolicyConfig
from services.agent.thread_detector import RuleThreadDetector
from interfaces.state import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, SessionRecap, SessionRecapItem, ThreadEvidence, ThreadKind,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _event(event_id: str, kind: AgentEventKind, text: str) -> GroundedEvent:
    return GroundedEvent(
        event_id, kind, AgentEventSource.CHAT, NOW, 1.0, {"text": text},
        EventProvenance("m4-continuity-test", source_event_id=event_id),
    )


def test_kể_tiếp_updates_one_grounded_thread_and_context() -> None:
    manager = OpenThreadManager(
        OpenThreadLimits(), clock=lambda: NOW, detector=RuleThreadDetector(),
    )
    manager.create(
        kind=ThreadKind.STORY,
        topic="Harry Potter",
        summary="viewer requested a Harry Potter story",
        evidence=ThreadEvidence(
            "sanitized-turn-10-user", "kể harry potter đi", "sanitized_fixture",
        ),
    )
    follow_up = _event("follow-up-1", AgentEventKind.CHAT_RECEIVED, "Kể tiếp đi")
    manager.handle_event(follow_up)
    threads = manager.snapshot()
    assert len(threads) == 1
    assert [item.source_event_id for item in threads[0].evidence] == [
        "sanitized-turn-10-user", "follow-up-1",
    ]

    state = AgentStateSnapshot(
        open_threads=threads,
        recent_events=(follow_up,),
        session_recap=SessionRecap((SessionRecapItem(
            "sanitized-turn-10-user", AgentEventKind.CHAT_RECEIVED,
            "Viewer: kể harry potter đi", NOW, "sanitized_fixture",
        ),)),
    )
    context = ConversationContextComposer(
        ConversationContextConfig(),
        repair_policy=ConversationRepairPolicy(
            RepairPolicyConfig(), clock=lambda: NOW + timedelta(minutes=1),
        ),
    ).render(state, "Kể tiếp đi")
    assert "sanitized-turn-10-user" in context
    assert "follow-up-1" in context
    assert "Repair policy" not in context


def test_missing_and_ambiguous_references_repair_without_inventing_identity() -> None:
    events = (
        _event("chat-a", AgentEventKind.CHAT_RECEIVED, "hôm nay vui không"),
        _event("chat-b", AgentEventKind.CHAT_RECEIVED, "kể harry potter đi"),
    )
    state = AgentStateSnapshot(recent_events=events)
    metrics = MetricsCollector()
    policy = ConversationRepairPolicy(
        RepairPolicyConfig(), clock=lambda: NOW + timedelta(minutes=1), metrics=metrics,
    )
    composer = ConversationContextComposer(
        ConversationContextConfig(), metrics=metrics, repair_policy=policy,
    )
    missing = composer.render(state, "Nãy cậu bảo trời đang mưa đúng không?")
    ambiguous = composer.render(state, "Ai nói vậy?")
    assert "Repair policy [missing_evidence" in missing
    assert "Repair policy [ambiguity" in ambiguous
    assert "ask" in ambiguous.lower()
    assert "viewer_id" not in ambiguous
    assert "Alice" not in ambiguous
    assert metrics.continuity_snapshot()["repairs"] == {
        "ambiguity": 1, "missing_evidence": 1,
    }
