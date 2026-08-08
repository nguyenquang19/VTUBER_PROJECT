from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.agent.agent_state import AgentStateLimits, AgentStateReducer
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
)

NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    kind: AgentEventKind,
    payload: dict,
    *,
    timestamp: datetime = NOW,
) -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=kind,
        source=AgentEventSource.CHAT,
        timestamp=timestamp,
        confidence=1.0,
        payload=payload,
        provenance=EventProvenance("test", source_event_id=event_id),
    )


@pytest.fixture
def reducer() -> AgentStateReducer:
    return AgentStateReducer(AgentStateLimits(
        recent_events_max=4,
        recent_event_ttl_s=60,
        open_threads_max=2,
        open_thread_ttl_s=30,
        payload_text_max_chars=80,
    ))


def test_grounded_event_is_deeply_immutable() -> None:
    event = _event("e1", AgentEventKind.CHAT_RECEIVED, {"text": "xin chào", "tags": ["a"]})
    with pytest.raises(TypeError):
        event.payload["text"] = "mutated"  # type: ignore[index]
    assert event.payload["tags"] == ("a",)


def test_chat_reply_follow_up_preserves_grounded_topic(reducer: AgentStateReducer) -> None:
    state = AgentStateSnapshot()
    first = _event("chat-1", AgentEventKind.CHAT_RECEIVED, {"text": "Mai thích cà phê nào?"})
    speech = _event(
        "speech-1", AgentEventKind.SPEECH_FINAL, {"text": "Tớ thích cà phê sữa."},
        timestamp=NOW + timedelta(seconds=1),
    )
    follow_up = _event(
        "chat-2", AgentEventKind.CHAT_RECEIVED, {"text": "Thế còn uống nóng?"},
        timestamp=NOW + timedelta(seconds=2),
    )
    for event in (first, speech, follow_up):
        state = reducer.reduce(state, event, now=event.timestamp)

    assert state.current_topic is not None
    assert state.current_topic.summary == "Mai thích cà phê nào?"
    assert state.current_topic.source_event_id == "chat-2"
    assert state.last_spoken_summary == "Tớ thích cà phê sữa."
    assert len(state.open_threads) == 1


def test_out_of_order_chat_does_not_replace_newer_topic(reducer: AgentStateReducer) -> None:
    newer = _event("new", AgentEventKind.CHAT_RECEIVED, {"text": "chủ đề mới"})
    older = _event(
        "old", AgentEventKind.CHAT_RECEIVED, {"text": "chủ đề cũ"},
        timestamp=NOW - timedelta(seconds=5),
    )
    state = reducer.reduce(AgentStateSnapshot(), newer, now=NOW)
    state = reducer.reduce(state, older, now=NOW)
    assert state.current_topic is not None
    assert state.current_topic.summary == "chủ đề mới"
    assert [event.event_id for event in state.recent_events] == ["old", "new"]


def test_recent_cap_and_open_thread_ttl_use_injected_clock(reducer: AgentStateReducer) -> None:
    state = AgentStateSnapshot()
    for index in range(6):
        event = _event(
            f"e{index}", AgentEventKind.CHAT_RECEIVED, {"text": f"hỏi {index}?"},
            timestamp=NOW + timedelta(seconds=index),
        )
        state = reducer.reduce(state, event, now=event.timestamp)
    assert len(state.recent_events) == 4
    assert len(state.open_threads) == 2

    late = _event(
        "late", AgentEventKind.SPEECH_FINAL, {"text": "xong"},
        timestamp=NOW + timedelta(seconds=40),
    )
    state = reducer.reduce(state, late, now=late.timestamp)
    assert state.open_threads == ()


def test_snapshot_to_dict_does_not_mutate_value_object() -> None:
    event = _event("e1", AgentEventKind.CHAT_RECEIVED, {"text": "grounded"})
    snapshot = AgentStateSnapshot(recent_events=(event,), environment_summary={"online": True})
    exported = snapshot.to_dict()
    exported["recent_events"][0]["payload"]["text"] = "fake"
    exported["environment_summary"]["online"] = False
    assert snapshot.recent_events[0].payload["text"] == "grounded"
    assert snapshot.environment_summary == {"online": True}
