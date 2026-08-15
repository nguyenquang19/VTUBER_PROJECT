from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.agent.conversation_context import (
    ConversationContextComposer, ConversationContextConfig,
)
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.repair_policy import ConversationRepairPolicy, RepairPolicyConfig
from services.agent.types import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance,
    GroundedEvent, OpenThread, SessionRecap, SessionRecapItem, ThreadEvidence,
    ThreadKind, TopicState,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _event(index: int, text: str) -> GroundedEvent:
    return GroundedEvent(
        f"event-{index}", AgentEventKind.CHAT_RECEIVED, AgentEventSource.CHAT,
        NOW + timedelta(seconds=index), 1.0, {"text": text},
        EventProvenance("context-test", source_event_id=f"source-{index}"),
    )


def _state() -> AgentStateSnapshot:
    events = tuple(_event(index, f"grounded coffee evidence {index}") for index in range(5))
    thread = OpenThread(
        "thread-1", "coffee", "unfinished coffee story", NOW, NOW,
        NOW + timedelta(minutes=5), kind=ThreadKind.STORY,
        evidence=(ThreadEvidence("event-1", "coffee story", "rule"),),
    )
    recap = SessionRecap((SessionRecapItem(
        "event-2", AgentEventKind.CHAT_RECEIVED, "Viewer: bounded recap", NOW,
        "context-test",
    ),))
    return AgentStateSnapshot(
        current_topic=TopicState("coffee", "event-0", NOW, 1.0),
        open_threads=(thread,), recent_events=events, session_recap=recap,
    )


def _goal() -> Goal:
    return Goal(
        "goal-1", GoalKind.CONTINUE_THREAD, GoalStatus.ACTIVE, 40,
        "continue grounded coffee thread", GoalSource.RULE, NOW,
        NOW + timedelta(minutes=5), ("speech completes thread",),
        parent_thread_id="thread-1",
    )


def test_composer_renders_topic_thread_goal_and_exactly_three_evidence() -> None:
    composer = ConversationContextComposer(
        ConversationContextConfig(1400, 3, 180),
        goal_provider=lambda: GoalSnapshot(active=_goal()),
    )
    context = composer.render(_state(), "coffee")
    assert "Current topic [event-0]" in context
    assert "Open thread [thread-1" in context
    assert "Active goal [goal-1" in context
    assert context.count("Evidence 1 ") == 1
    assert context.count("Evidence 2 ") == 1
    assert context.count("Evidence 3 ") == 1
    assert "source_id=source-" in context
    assert "Bounded recap:" in context


def test_context_respects_budget_and_marks_missing_evidence_without_invention() -> None:
    composer = ConversationContextComposer(ConversationContextConfig(400, 3, 80))
    context = composer.render(AgentStateSnapshot(), "nãy cậu bảo gì")
    assert len(context) <= 400
    assert "Current topic: none recorded" in context
    assert "Open thread: none recorded" in context
    assert "Active goal: none recorded" in context
    assert "Evidence 1: none recorded" in context
    assert "Evidence 3: none recorded" in context


async def test_context_composer_service_lifecycle() -> None:
    composer = ConversationContextComposer(ConversationContextConfig())
    assert not (await composer.health_check()).is_ok
    await composer.start()
    assert (await composer.health_check()).is_ok
    await composer.stop()


def test_repair_instruction_is_rendered_inside_grounded_context() -> None:
    policy = ConversationRepairPolicy(
        RepairPolicyConfig(), clock=lambda: NOW + timedelta(minutes=1),
    )
    composer = ConversationContextComposer(
        ConversationContextConfig(1400, 3, 180), repair_policy=policy,
    )
    context = composer.render(
        AgentStateSnapshot(recent_events=(_event(1, "coffee only"),)),
        "Nãy cậu bảo trời mưa đúng không?",
    )
    assert "Repair policy [missing_evidence" in context
    assert "not certain" in context
