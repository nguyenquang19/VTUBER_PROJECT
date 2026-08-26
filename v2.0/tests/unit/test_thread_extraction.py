from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from interfaces.llm import LLMToken
from services.agent.thread_extraction import PostHocThreadExtractor
from interfaces.state import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, OpenThread,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class FakeLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[Any] = []

    async def generate_stream(self, request):
        self.requests.append(request)
        yield LLMToken(request_id=request.request_id, token=self.output, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def _event() -> GroundedEvent:
    return GroundedEvent(
        "speech-1", AgentEventKind.SPEECH_FINAL, AgentEventSource.LLM, NOW, 1.0,
        {"text": "Tớ đang kể chuyện quán cà phê cũ."},
        EventProvenance("test", source_event_id="speech-1"),
    )


def _state() -> AgentStateSnapshot:
    thread = OpenThread(
        "thread-1", "cà phê", "chuyện cà phê", NOW, NOW,
        NOW + timedelta(minutes=5),
    )
    return AgentStateSnapshot(recent_events=(_event(),), open_threads=(thread,))


def _extractor(output: str, enabled: bool = True) -> tuple[PostHocThreadExtractor, FakeLLM]:
    llm = FakeLLM(output)
    return PostHocThreadExtractor(llm, "strict", enabled=enabled), llm


async def test_disabled_extractor_never_calls_llm() -> None:
    extractor, llm = _extractor("{}", enabled=False)
    assert await extractor.propose(_event(), _state()) is None
    assert llm.requests == []


async def test_valid_grounded_extraction_is_accepted() -> None:
    output = json.dumps({
        "operation": "create", "kind": "story", "topic": "quán cà phê",
        "summary": "Mai đang kể chuyện", "source_event_id": "speech-1",
        "evidence_excerpt": "chuyện quán cà phê cũ", "target_thread_id": None,
        "reason": None,
    })
    extractor, llm = _extractor(output)
    result = await extractor.propose(_event(), _state())
    assert result is not None
    assert result.source_event_id == "speech-1"
    assert llm.requests[0].messages[0].role == "system"


@pytest.mark.parametrize("patch", [
    {"source_event_id": "invented"},
    {"evidence_excerpt": "words not in source"},
    {"operation": "update", "target_thread_id": "invented"},
    {"priority": 999},
])
async def test_ungrounded_or_extra_fields_are_rejected(patch: dict[str, Any]) -> None:
    data = {
        "operation": "create", "kind": "story", "topic": "cà phê",
        "summary": "story", "source_event_id": "speech-1",
        "evidence_excerpt": "chuyện quán cà phê cũ", "target_thread_id": None,
        "reason": None,
    }
    data.update(patch)
    extractor, _llm = _extractor(json.dumps(data))
    assert await extractor.propose(_event(), _state()) is None


async def test_service_lifecycle_and_toggle() -> None:
    extractor, _llm = _extractor("{}", enabled=False)
    assert not (await extractor.health_check()).is_ok
    await extractor.start()
    extractor.set_enabled(True)
    assert extractor.enabled and (await extractor.health_check()).is_ok
    await extractor.stop()
