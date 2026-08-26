from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.agent.conversation_move_planner import (
    ConversationMoveConfig, ConversationMovePlanner,
)
from services.agent.open_thread_manager import OpenThreadLimits, OpenThreadManager
from services.agent.thread_detector import RuleThreadDetector
from services.agent.topic_matcher import LexicalTopicMatcher, TopicMatcherConfig
from interfaces.state import (
    AgentEventKind, AgentEventSource, ConversationMove, EventProvenance,
    GroundedEvent, OpenThread, ThreadEvidence, ThreadKind, ThreadOperation,
    ThreadSpeaker, ThreadStatus,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _event(event_id: str, text: str) -> GroundedEvent:
    return GroundedEvent(
        event_id, AgentEventKind.CHAT_RECEIVED, AgentEventSource.CHAT,
        NOW, 1.0, {"text": text}, EventProvenance("test", event_id),
    )


def test_long_thread_parks_resumes_and_bounds_grounded_contributions() -> None:
    clock = Clock()
    manager = OpenThreadManager(
        OpenThreadLimits(
            max_open=2, ttl_seconds=100, evidence_max=5, field_max_chars=80,
            terminal_history_max=4, contributions_max=2, open_questions_max=1,
            park_after_seconds=10,
        ),
        clock=clock,
        move_planner=ConversationMovePlanner(ConversationMoveConfig()),
    )
    thread = manager.create(
        kind=ThreadKind.QUESTION, topic="coffee", summary="dark roast coffee",
        evidence=ThreadEvidence("chat-1", "dark roast coffee", "rule"),
        speaker=ThreadSpeaker.VIEWER,
    )
    assert thread and thread.next_move is ConversationMove.DEEPEN
    clock.now += timedelta(seconds=11)
    assert manager.snapshot()[0].status is ThreadStatus.PARKED
    assert manager.snapshot()[0].next_move is ConversationMove.RESUME

    for event_id, text in (
        ("chat-2", "dark roast coffee"),
        ("chat-3", "medium roast coffee"),
        ("chat-4", "light roast coffee"),
    ):
        assert manager.update(
            thread.thread_id, summary=text,
            evidence=ThreadEvidence(event_id, text, "rule"),
            speaker=ThreadSpeaker.VIEWER,
        )
    resumed = manager.snapshot()[0]
    assert resumed.status is ThreadStatus.ACTIVE
    assert [item.source_event_id for item in resumed.viewer_contributions] == [
        "chat-3", "chat-4",
    ]


def test_detector_merges_related_topic_and_rejects_cross_topic_match() -> None:
    matcher = LexicalTopicMatcher(TopicMatcherConfig(min_score=0.3))
    detector = RuleThreadDetector(matcher=matcher)
    coffee_thread = OpenThread(
        "coffee", "dark roast coffee", "coffee shop discussion",
        NOW, NOW, NOW + timedelta(minutes=5),
    )
    related = detector.detect(
        _event("same", "is that coffee dark roast?"), (coffee_thread,),
    )[0]
    assert related.operation is ThreadOperation.UPDATE
    assert related.target_thread_id == "coffee"

    unrelated = detector.detect(
        _event("other", "new horror game looks scary?"), (coffee_thread,),
    )[0]
    assert unrelated.operation is ThreadOperation.CREATE
    assert unrelated.target_thread_id is None


def test_delivered_statement_with_quoted_question_does_not_enter_waiting_state() -> None:
    detector = RuleThreadDetector()
    thread = OpenThread(
        "question", "coffee question?", "viewer asked coffee question?",
        NOW, NOW, NOW + timedelta(minutes=5),
    )
    event = GroundedEvent(
        "speech-1", AgentEventKind.SPEECH_COMPLETED, AgentEventSource.DIRECTOR,
        NOW, 1.0,
        {"text": "Tớ sẽ đào sâu câu 'coffee question?' trước.", "thread_id": "question"},
        EventProvenance("director_loop", "speech-1"),
    )
    signal = detector.detect(event, (thread,))[0]
    assert signal.status is ThreadStatus.ACTIVE
    assert signal.is_open_question is False


def test_unambiguous_generic_follow_up_resumes_single_parked_thread() -> None:
    detector = RuleThreadDetector()
    parked = OpenThread(
        "parked", "coffee story", "unfinished coffee story",
        NOW, NOW, NOW + timedelta(minutes=5), status=ThreadStatus.PARKED,
    )
    signal = detector.detect(_event("resume", "continue please"), (parked,))[0]
    assert signal.operation is ThreadOperation.UPDATE
    assert signal.target_thread_id == parked.thread_id
    assert signal.status is ThreadStatus.ACTIVE
