from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.operations.metrics import MetricsCollector
from services.state.event_ledger import EventLedger
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)

NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _event(event_id: str, offset_s: float = 0) -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.CHAT,
        timestamp=NOW + timedelta(seconds=offset_s),
        confidence=1.0,
        payload={"text": event_id},
        provenance=EventProvenance("test", source_event_id=event_id),
    )


def test_append_out_of_order_returns_chronological_view() -> None:
    clock = Clock()
    ledger = EventLedger(4, 60, 60, clock=clock)
    assert ledger.append(_event("new", 3)) is True
    assert ledger.append(_event("old", 1)) is True
    assert [event.event_id for event in ledger.recent()] == ["old", "new"]


def test_duplicate_is_dropped_and_counted() -> None:
    metrics = MetricsCollector()
    ledger = EventLedger(4, 60, 60, clock=Clock(), metrics=metrics)
    assert ledger.append(_event("same")) is True
    assert ledger.append(_event("same")) is False
    assert ledger.get_metrics()["agent_events_dropped_by_reason"] == {"duplicate": 1}
    assert metrics.agent_snapshot() == {
        "accepted_total": 1,
        "dropped_total": 1,
        "dropped_by_reason": {"duplicate": 1},
    }


def test_cap_rejects_out_of_order_event_older_than_retained_window() -> None:
    ledger = EventLedger(2, 60, 60, clock=Clock())
    assert ledger.append(_event("a", 1)) is True
    assert ledger.append(_event("b", 2)) is True
    assert ledger.append(_event("too-old", 0)) is False
    assert [event.event_id for event in ledger.recent()] == ["a", "b"]
    assert ledger.get_metrics()["agent_events_dropped_by_reason"]["capacity"] == 1


def test_ttl_and_dedup_expire_with_injected_clock() -> None:
    clock = Clock()
    ledger = EventLedger(4, 10, 5, clock=clock)
    assert ledger.append(_event("e1")) is True
    clock.now = NOW + timedelta(seconds=11)
    assert ledger.recent() == ()
    assert ledger.append(_event("e1", 11)) is True


def test_expired_event_is_rejected() -> None:
    clock = Clock()
    clock.now = NOW + timedelta(seconds=20)
    ledger = EventLedger(4, 10, 30, clock=clock)
    assert ledger.append(_event("old")) is False
    assert ledger.get_metrics()["agent_events_dropped_by_reason"] == {"expired": 1}
