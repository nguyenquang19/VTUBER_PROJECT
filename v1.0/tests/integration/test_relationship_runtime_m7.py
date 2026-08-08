from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from interfaces.input import EventSource, InputEvent
from services.input.chat_router import ChatRouter


class _Relationship:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def observe_interaction(self, **kwargs):
        self.calls.append(kwargs)


def test_chat_router_forwards_grounded_identity_without_storing_alias() -> None:
    relationship = _Relationship()
    router = object.__new__(ChatRouter)
    router._relationship_manager = relationship
    router._log = SimpleNamespace(warning=lambda *args, **kwargs: None)
    event = InputEvent(
        event_id="youtube-chat-1",
        source=EventSource.CHAT_YOUTUBE,
        content="hello",
        timestamp=datetime(2026, 8, 8, tzinfo=timezone.utc),
        user_id="raw-platform-id",
        user_name="Display Name",
    )
    router._record_relationship_interaction(event)
    assert relationship.calls == [{
        "raw_viewer_id": "raw-platform-id",
        "event_id": "youtube-chat-1",
        "occurred_at": event.timestamp,
    }]
    assert "user_name" not in relationship.calls[0]


def test_relationship_failure_isolated_from_chat_router() -> None:
    class Broken:
        def observe_interaction(self, **kwargs):
            raise RuntimeError("db unavailable")

    router = object.__new__(ChatRouter)
    router._relationship_manager = Broken()
    router._log = SimpleNamespace(warning=lambda *args, **kwargs: None)
    event = SimpleNamespace(
        event_id="e1", user_id="raw", timestamp=datetime.now(timezone.utc),
    )
    router._record_relationship_interaction(event)
