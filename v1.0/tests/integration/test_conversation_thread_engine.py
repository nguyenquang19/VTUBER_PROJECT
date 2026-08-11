from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.agent.conversation_move_planner import (
    ConversationMoveConfig, ConversationMovePlanner,
)
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.open_thread_manager import OpenThreadLimits, OpenThreadManager
from services.agent.thread_detector import RuleThreadDetector
from services.agent.topic_matcher import LexicalTopicMatcher, TopicMatcherConfig
from services.agent.types import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, ThreadEvidence, ThreadKind, ThreadStatus,
)
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction
from services.director.director_loop import DirectorLoop

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _manager() -> OpenThreadManager:
    matcher = LexicalTopicMatcher(TopicMatcherConfig(min_score=0.3))
    planner = ConversationMovePlanner(ConversationMoveConfig())
    detector = RuleThreadDetector(matcher=matcher)
    return OpenThreadManager(
        OpenThreadLimits(), detector=detector, matcher=matcher, move_planner=planner,
        clock=lambda: NOW,
    )


def _goal(thread_id: str) -> Goal:
    return Goal(
        "goal-thread", GoalKind.CONTINUE_THREAD, GoalStatus.ACTIVE, 40,
        "continue grounded thread", GoalSource.RULE, NOW, NOW + timedelta(minutes=5),
        ("delivered speech addresses thread",), parent_thread_id=thread_id,
        metadata={"source_event_id": "chat-1"},
    )


class _State:
    def __init__(self, manager: OpenThreadManager) -> None:
        self.manager = manager
        self.events: list[GroundedEvent] = []

    def snapshot(self) -> AgentStateSnapshot:
        return AgentStateSnapshot(
            open_threads=self.manager.snapshot(), active_goal_ref="goal-thread",
        )

    def record(self, event: GroundedEvent) -> bool:
        self.events.append(event)
        self.manager.handle_event(event)
        return True


class _Runner:
    session_id = "test-session"

    def __init__(self) -> None:
        self.committed: list[str] = []

    async def run_directed_turn(self, _request_id: str, _context: str):
        return type("Parsed", (), {"text": "Tớ thấy vị rang đậm tạo điểm khác biệt."})()

    async def run_ambient_turn(self, _request_id: str, _prompt: str):
        return type("Parsed", (), {
            "text": "Tớ nối thêm một điểm mới về vị rang.", "ok": True,
        })()

    def commit_self_talk(self, text: str) -> None:
        self.committed.append(text)


class _Director:
    def mark_spoke(self, _action, _now) -> None:
        pass

    def mark_proactive_used(self, _decision, _now) -> None:
        pass


class _Decision:
    action = DirectorAction.CONTINUE_THREAD
    goal_id = "goal-thread"


class _Autonomy:
    def force_generate_for(self, _category, _mood, _context):
        return type("Material", (), {"prompt_text": "continue grounded topic"})()

    def force_generate(self, _mood, _context):
        return type("Material", (), {"prompt_text": "continue grounded topic"})()

    def check_dedup(self, _text: str) -> bool:
        return False

    def on_self_spoke(self, _text: str) -> None:
        pass


def _loop_and_input():
    manager = _manager()
    thread = manager.create(
        kind=ThreadKind.QUESTION, topic="cà phê rang",
        summary="Cậu thích cà phê rang kiểu nào?",
        evidence=ThreadEvidence("chat-1", "Cậu thích cà phê rang kiểu nào?", "rule"),
    )
    assert thread is not None
    state = _State(manager)
    goal = _goal(thread.thread_id)
    value = DirectorInput(
        now=1.0,
        agent_state=AgentStateSnapshot(
            open_threads=manager.snapshot(), active_goal_ref=goal.goal_id,
        ),
        goals=GoalSnapshot(active=goal),
    )
    loop = DirectorLoop(
        _Director(), object(), object(), _Runner(), agent_state=state, clock=lambda: 1.0,
    )
    return loop, value, manager, state


@pytest.mark.asyncio
async def test_thread_does_not_advance_without_delivery_sink_or_on_failed_delivery() -> None:
    loop, value, manager, state = _loop_and_input()
    before = manager.snapshot()[0]
    assert await loop._exec_goal_action(_Decision(), 1.0, value) is False
    assert manager.snapshot()[0].move_count == before.move_count
    assert state.events == []

    async def failed(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id, delivered=False, mode=TTSDeliveryMode.NONE,
        )

    loop._speak = failed
    assert await loop._exec_goal_action(_Decision(), 2.0, value) is False
    assert manager.snapshot()[0].move_count == before.move_count
    assert state.events == []


@pytest.mark.asyncio
async def test_delivered_thread_move_commits_claim_and_progress_once() -> None:
    loop, value, manager, state = _loop_and_input()

    async def delivered(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id, delivered=True, mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1, sentences_delivered=1, subtitle_sentences=1,
        )

    loop._speak = delivered
    assert await loop._exec_goal_action(_Decision(), 1.0, value) is True
    thread = manager.snapshot()[0]
    assert thread.move_count == 1
    assert thread.claims[-1].text == "Tớ thấy vị rang đậm tạo điểm khác biệt."
    assert thread.status is ThreadStatus.ACTIVE
    assert len(state.events) == 1


@pytest.mark.asyncio
async def test_proactive_open_thread_follow_up_uses_same_delivery_commit_boundary() -> None:
    loop, _value, manager, state = _loop_and_input()
    loop._autonomy = _Autonomy()
    thread = manager.snapshot()[0]
    decision = type("Decision", (), {
        "action": DirectorAction.FOLLOW_UP,
        "proactive_source": "open_thread",
        "proactive_source_id": thread.thread_id,
        "proactive_summary": thread.summary,
        "proactive_category": "follow_up_topic",
    })()

    assert await loop._exec_self_talk(decision, 1.0) is False
    assert manager.snapshot()[0].move_count == 0

    async def delivered(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id, delivered=True, mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1, sentences_delivered=1, subtitle_sentences=1,
        )

    loop._speak = delivered
    assert await loop._exec_self_talk(decision, 2.0) is True
    assert manager.snapshot()[0].move_count == 1
    assert len(state.events) == 2  # speech_completed + self_talk_completed
