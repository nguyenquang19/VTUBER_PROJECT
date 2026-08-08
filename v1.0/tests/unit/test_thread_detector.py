from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.agent.thread_detector import RuleThreadDetector
from services.agent.types import (
    AgentEventKind, AgentEventSource, EventProvenance, GroundedEvent, OpenThread,
    ThreadKind, ThreadOperation,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _event(event_id: str, kind: AgentEventKind, text: str) -> GroundedEvent:
    return GroundedEvent(
        event_id, kind, AgentEventSource.CHAT, NOW, 1.0, {"text": text},
        EventProvenance("test", source_event_id=event_id),
    )


def _thread(kind: ThreadKind = ThreadKind.STORY) -> OpenThread:
    return OpenThread(
        "thread-1", "chuyện cà phê", "tớ đang kể chuyện cà phê",
        NOW, NOW, NOW + timedelta(minutes=5), kind=kind,
    )


def test_detects_question_promise_and_story_with_readable_evidence() -> None:
    detector = RuleThreadDetector()
    cases = (
        (_event("q", AgentEventKind.CHAT_RECEIVED, "Mai thích món nào?"), ThreadKind.QUESTION),
        (_event("p", AgentEventKind.SPEECH_FINAL, "Lát nữa tớ sẽ kể kết quả."), ThreadKind.PROMISE),
        (_event("s", AgentEventKind.SPEECH_FINAL, "Để tớ kể chuyện này."), ThreadKind.STORY),
    )
    for event, expected in cases:
        signal = detector.detect(event, ())[0]
        assert signal.operation is ThreadOperation.CREATE
        assert signal.kind is expected
        assert signal.evidence.source_event_id == event.event_id
        assert signal.evidence.excerpt == event.payload["text"]


def test_follow_up_updates_and_explicit_completion_resolves_existing_thread() -> None:
    detector = RuleThreadDetector()
    update = detector.detect(
        _event("u", AgentEventKind.CHAT_RECEIVED, "Kể tiếp đi"), (_thread(),),
    )[0]
    assert update.operation is ThreadOperation.UPDATE
    assert update.target_thread_id == "thread-1"
    resolved = detector.detect(
        _event("r", AgentEventKind.SPEECH_FINAL, "Tớ kể xong chuyện này rồi."),
        (_thread(),),
    )[0]
    assert resolved.operation is ThreadOperation.RESOLVE
    assert resolved.reason == "explicit_completion"


def test_non_conversational_event_or_plain_statement_produces_no_signal() -> None:
    detector = RuleThreadDetector()
    assert detector.detect(
        _event("plain", AgentEventKind.CHAT_RECEIVED, "xin chào"), (),
    ) == ()
    assert detector.detect(
        _event("env", AgentEventKind.ENVIRONMENT_OBSERVED, "ignored"), (),
    ) == ()
