from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.agent.conversation_move_planner import (
    ConversationMoveConfig, ConversationMovePlanner,
)
from services.agent.agent_state import AgentState, AgentStateLimits, AgentStateReducer
from services.agent.agenda_policy import AgendaPolicy, AgendaPolicyConfig
from services.agent.event_ledger import EventLedger
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_types import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
    ShortIntention,
    ShortIntentionStatus,
)
from services.agent.open_thread_manager import OpenThreadLimits, OpenThreadManager
from services.agent.thread_detector import RuleThreadDetector
from services.agent.topic_matcher import LexicalTopicMatcher, TopicMatcherConfig
from services.agent.types import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, ConversationMove,
    EventProvenance, GroundedEvent, ThreadEvidence, ThreadKind, ThreadStatus,
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


def _goal_snapshot(goal: Goal) -> GoalSnapshot:
    intention = ShortIntention(
        intention_id=f"intention:{goal.goal_id}:1",
        goal_id=goal.goal_id,
        status=ShortIntentionStatus.ACTIVE,
        step_index=0,
        step_count=len(goal.steps),
        step=goal.steps[0],
        created_at=goal.created_at,
        updated_at=goal.created_at,
        expires_at=goal.expires_at,
        reason_code="activated",
    )
    return GoalSnapshot(active=goal, current_intention=intention)


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
    def get_metrics(self) -> dict[str, object]:
        return {}

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
        goals=_goal_snapshot(goal),
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
async def test_exhausted_duplicate_delivers_same_parent_park_instead_of_resolving() -> None:
    loop, value, manager, state = _loop_and_input()
    repeated = "Tớ thấy vị rang đậm tạo điểm khác biệt."
    loop._speech_dedup.record(repeated)
    loop._speech_dedup_max_regenerations = 0

    async def delivered(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id, delivered=True, mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1, sentences_delivered=1, subtitle_sentences=1,
        )

    loop._speak = delivered
    assert await loop._exec_goal_action(_Decision(), 1.0, value) is True
    thread = manager.snapshot()[0]
    assert thread.status is ThreadStatus.PARKED
    assert thread.last_move is ConversationMove.PARK
    assert state.events[-1].payload["conversation_move"] == "park"
    assert loop.get_metrics()["director_thread_forced_park_total"] == 1


@pytest.mark.asyncio
async def test_failed_forced_park_keeps_thread_and_goal_boundary_unchanged() -> None:
    loop, value, manager, state = _loop_and_input()
    loop._speech_dedup.record("Tớ thấy vị rang đậm tạo điểm khác biệt.")
    loop._speech_dedup_max_regenerations = 0

    async def failed(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id, delivered=False, mode=TTSDeliveryMode.NONE,
        )

    loop._speak = failed
    before = manager.snapshot()[0]
    assert await loop._exec_goal_action(_Decision(), 1.0, value) is False
    after = manager.snapshot()[0]
    assert after.thread_id == before.thread_id
    assert after.status is ThreadStatus.ACTIVE
    assert after.move_count == before.move_count
    assert state.events == []


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


def test_delivered_thread_runs_to_park_before_another_goal_can_activate() -> None:
    matcher = LexicalTopicMatcher(TopicMatcherConfig(min_score=0.3))
    planner = ConversationMovePlanner(ConversationMoveConfig(
        summarize_after_moves=2,
        invite_after_moves=2,
        compare_after_viewer_contributions=2,
    ))
    thread_manager = OpenThreadManager(
        OpenThreadLimits(),
        detector=RuleThreadDetector(matcher=matcher),
        matcher=matcher,
        move_planner=planner,
        clock=lambda: NOW,
    )
    state = AgentState(
        AgentStateReducer(AgentStateLimits(64, 3600, 8, 900, 320)),
        EventLedger(64, 3600, 3600, clock=lambda: NOW),
        clock=lambda: NOW,
        thread_manager=thread_manager,
    )
    priorities = {
        GoalKind.ACK_DONATION: 100,
        GoalKind.WAIT_FOR_CHAT_ANSWER: 60,
        GoalKind.CONTINUE_THREAD: 40,
        GoalKind.ANSWER_FOLLOW_UP: 70,
        GoalKind.OPERATOR_PINNED: 90,
    }
    policy = AgendaPolicy(AgendaPolicyConfig(
        priorities=priorities,
        ttl_seconds={kind: 300 for kind in GoalKind},
    ), clock=lambda: NOW)
    goals = GoalManager(
        GoalLimits(16, 8, 32, 240),
        clock=lambda: NOW,
        agenda_policy=policy,
        on_active_changed=state.set_active_goal_ref,
    )
    state.add_event_listener(goals.handle_event)
    chat = GroundedEvent(
        event_id="agent:chat:a",
        kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.YOUTUBE,
        timestamp=NOW,
        confidence=1.0,
        payload={"text": "Mai nghĩ sao về cà phê rang đậm?"},
        provenance=EventProvenance("test", source_event_id="a"),
    )
    assert state.record(chat)
    thread_id = state.snapshot().open_threads[0].thread_id
    assert goals.snapshot().active is not None
    assert goals.snapshot().active.metadata["source_delivered"] is False
    assert goals.focus_delivered_thread(
        thread_id, source_event_ids={"agent:chat:a"},
    ) == 1

    delivered_moves: list[str] = []
    for index, expected in enumerate(("deepen", "clarify", "summarize", "park")):
        active = goals.snapshot().active
        intention = goals.snapshot().current_intention
        assert active is not None
        assert intention is not None
        thread = state.snapshot().open_threads[0]
        assert thread.thread_id == thread_id
        assert thread.next_move is not None
        assert thread.next_move.value == expected
        delivered_moves.append(expected)
        assert state.record(GroundedEvent(
            event_id=f"agent:speech_completed:{index}",
            kind=AgentEventKind.SPEECH_COMPLETED,
            source=AgentEventSource.DIRECTOR,
            timestamp=NOW,
            confidence=1.0,
            payload={
                "action": "continue_thread",
                "goal_id": active.goal_id,
                "intention_id": intention.intention_id,
                "thread_id": thread_id,
                "conversation_move": expected,
                "text": f"delivered {expected}",
            },
            provenance=EventProvenance("test", source_event_id=f"goal-{index}"),
        ))

    assert delivered_moves == ["deepen", "clarify", "summarize", "park"]
    assert state.snapshot().open_threads[0].status is ThreadStatus.PARKED
    assert goals.snapshot().active is None
