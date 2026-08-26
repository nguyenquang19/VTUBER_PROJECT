from __future__ import annotations

from datetime import datetime, timezone

from services.operations.metrics import MetricsCollector
from services.agent.session_recap import SessionRecapLimits, SessionRecapManager
from interfaces.state import (
    AgentEventKind, AgentEventSource, EventProvenance, GroundedEvent,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _event(index: int, kind: AgentEventKind, text: str) -> GroundedEvent:
    return GroundedEvent(
        f"e{index}", kind, AgentEventSource.CHAT, NOW, 1.0, {"text": text},
        EventProvenance("recap-test", source_event_id=f"e{index}"),
    )


def test_recap_is_bounded_by_items_chars_and_preserves_provenance() -> None:
    manager = SessionRecapManager(SessionRecapLimits(3, 90, 30))
    for index in range(6):
        manager.handle_event(_event(
            index, AgentEventKind.CHAT_RECEIVED,
            f"message {index} with bounded continuation detail",
        ))
    recap = manager.snapshot()
    assert len(recap.items) <= 3
    assert recap.total_chars <= 90
    assert recap.items[-1].source_event_id == "e5"
    assert recap.items[-1].producer == "recap-test"
    assert "bounded continuation" not in recap.items[-1].summary  # item cap applied


def test_recap_ignores_non_dialogue_events_and_never_keeps_full_transcript() -> None:
    manager = SessionRecapManager(SessionRecapLimits(2, 80, 25))
    manager.handle_event(_event(1, AgentEventKind.DIRECTOR_ACTION, "internal action"))
    manager.handle_event(_event(2, AgentEventKind.SPEECH_FINAL, "x" * 500))
    recap = manager.snapshot()
    assert len(recap.items) == 1
    assert len(recap.items[0].summary) <= 30
    assert "x" * 100 not in recap.items[0].summary


def test_recap_chars_metric_is_updated() -> None:
    metrics = MetricsCollector()
    manager = SessionRecapManager(SessionRecapLimits(3, 100, 40), metrics=metrics)
    manager.handle_event(_event(1, AgentEventKind.CHAT_RECEIVED, "grounded recap"))
    assert b"mai_session_recap_chars" in metrics.prometheus_text()
    assert manager.get_metrics()["session_recap_chars"] > 0


async def test_recap_service_lifecycle() -> None:
    manager = SessionRecapManager(SessionRecapLimits())
    assert not (await manager.health_check()).is_ok
    await manager.start()
    assert (await manager.health_check()).is_ok
    await manager.stop()
