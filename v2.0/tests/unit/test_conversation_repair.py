from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.operations.metrics import MetricsCollector
from services.agent.repair_policy import (
    ConversationRepairPolicy, RepairKind, RepairPolicyConfig,
)
from interfaces.state import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, OpenThread,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _event(index: int, text: str, **payload: str) -> GroundedEvent:
    return GroundedEvent(
        f"e{index}", AgentEventKind.CHAT_RECEIVED, AgentEventSource.CHAT,
        NOW + timedelta(seconds=index), 1.0, {"text": text, **payload},
        EventProvenance("repair-test", source_event_id=f"e{index}"),
    )


def _policy(metrics=None) -> ConversationRepairPolicy:
    return ConversationRepairPolicy(
        RepairPolicyConfig(2, 1800), clock=lambda: NOW + timedelta(minutes=5),
        metrics=metrics,
    )


def test_ambiguous_speaker_asks_instead_of_inventing_viewer() -> None:
    state = AgentStateSnapshot(recent_events=(
        _event(1, "first claim"), _event(2, "second claim"),
    ))
    decision = _policy().decide(state, "Ai nói vậy?")
    assert decision and decision.kind is RepairKind.AMBIGUITY
    assert decision.evidence_ids == ("e1", "e2")
    assert "clarifying question" in decision.instruction


def test_missing_prior_statement_requires_uncertainty() -> None:
    decision = _policy().decide(
        AgentStateSnapshot(recent_events=(_event(1, "coffee is good"),)),
        "Nãy cậu bảo trời đang mưa đúng không?",
    )
    assert decision and decision.kind is RepairKind.MISSING_EVIDENCE
    assert "not certain" in decision.instruction


def test_matching_prior_statement_needs_no_repair() -> None:
    decision = _policy().decide(
        AgentStateSnapshot(recent_events=(_event(1, "trời đang mưa"),)),
        "Nãy cậu bảo trời đang mưa đúng không?",
    )
    assert decision is None


def test_conflicting_fact_does_not_choose_a_value() -> None:
    state = AgentStateSnapshot(recent_events=(
        _event(1, "tôi tên An", viewer_alias="same-viewer"),
        _event(2, "tôi tên Bình", viewer_alias="same-viewer"),
    ))
    decision = _policy().decide(state, "Tên của viewer đó là gì?")
    assert decision and decision.kind is RepairKind.CONFLICT
    assert set(decision.evidence_ids) == {"e1", "e2"}
    assert "Do not choose" in decision.instruction


def test_ambiguous_thread_reference_and_repair_metric() -> None:
    threads = tuple(OpenThread(
        f"t{i}", f"topic {i}", f"summary {i}", NOW, NOW,
        NOW + timedelta(minutes=10),
    ) for i in range(3))
    metrics = MetricsCollector()
    decision = _policy(metrics).decide(
        AgentStateSnapshot(open_threads=threads), "Kể tiếp chuyện đó đi",
    )
    assert decision and decision.kind is RepairKind.AMBIGUITY
    assert b'mai_conversation_repairs_total{kind="ambiguity"} 1.0' in metrics.prometheus_text()


async def test_repair_service_lifecycle() -> None:
    policy = _policy()
    assert not (await policy.health_check()).is_ok
    await policy.start()
    assert (await policy.health_check()).is_ok
    await policy.stop()
