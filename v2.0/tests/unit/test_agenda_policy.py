from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from interfaces.animation import MoodState
from services.agent.agenda_policy import AgendaPolicy, AgendaPolicyConfig
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
    OpenThread,
)
from services.agent.mood_policy import MoodActionPolicy, MoodPolicyConfig

NOW = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)


def _policy() -> AgendaPolicy:
    kinds = list(GoalKind)
    return AgendaPolicy(AgendaPolicyConfig(
        priorities={kind: {GoalKind.ACK_DONATION: 100}.get(kind, 50) for kind in kinds},
        ttl_seconds={kind: 60 for kind in kinds},
    ), clock=lambda: NOW)


def _event(kind: AgentEventKind, event_id: str, text: str, **payload) -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=kind,
        source=AgentEventSource.CHAT,
        timestamp=NOW,
        confidence=1.0,
        payload={"text": text, **payload},
        provenance=EventProvenance("test", source_event_id=event_id),
    )


def _waiting() -> Goal:
    return Goal(
        goal_id="wait-1", kind=GoalKind.WAIT_FOR_CHAT_ANSWER,
        status=GoalStatus.ACTIVE, priority=60, reason="asked",
        source=GoalSource.RULE, created_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        success_conditions=("chat reply",), parent_thread_id="thread-1",
    )


def test_donation_factory_is_p0_and_grounded() -> None:
    event = _event(
        AgentEventKind.DONATION_RECEIVED, "don-1", "ủng hộ Mai",
        viewer_alias="Lan", amount_vnd=50000,
    )
    goal = _policy().candidates_for(event, AgentStateSnapshot(), GoalSnapshot())[0]
    assert goal.kind is GoalKind.ACK_DONATION
    assert goal.priority == 100
    assert goal.metadata["source_event_id"] == "don-1"
    assert goal.metadata["viewer_alias"] == "Lan"


def test_question_speech_creates_short_wait_goal() -> None:
    event = _event(AgentEventKind.SPEECH_FINAL, "speech-1", "Chat thích món nào?")
    goal = _policy().candidates_for(event, AgentStateSnapshot(), GoalSnapshot())[0]
    assert goal.kind is GoalKind.WAIT_FOR_CHAT_ANSWER
    assert goal.expires_at == NOW + timedelta(seconds=60)
    assert goal.metadata["question"] == "Chat thích món nào?"


def test_non_question_speech_creates_nothing() -> None:
    event = _event(AgentEventKind.SPEECH_FINAL, "speech-1", "Tớ hiểu rồi.")
    assert _policy().candidates_for(event, AgentStateSnapshot(), GoalSnapshot()) == ()


def test_delivered_reply_echoing_viewer_question_does_not_create_wait_goal() -> None:
    reply = _event(
        AgentEventKind.SPEECH_COMPLETED, "speech-reply", "Viewer asked coffee?",
        action="read_chat", expects_chat_answer=False,
    )
    assert _policy().candidates_for(reply, AgentStateSnapshot(), GoalSnapshot()) == ()

    invitation = _event(
        AgentEventKind.SPEECH_COMPLETED, "speech-invite", "Chat prefers coffee?",
        action="continue_thread", expects_chat_answer=True,
    )
    goal = _policy().candidates_for(
        invitation, AgentStateSnapshot(), GoalSnapshot(),
    )[0]
    assert goal.kind is GoalKind.WAIT_FOR_CHAT_ANSWER


def test_chat_while_waiting_creates_answer_follow_up() -> None:
    event = _event(AgentEventKind.CHAT_RECEIVED, "chat-2", "Tớ chọn cà phê")
    goal = _policy().candidates_for(
        event, AgentStateSnapshot(), GoalSnapshot(active=_waiting()),
    )[0]
    assert goal.kind is GoalKind.ANSWER_FOLLOW_UP
    assert goal.parent_thread_id == "thread-1"
    assert goal.metadata["waiting_goal_id"] == "wait-1"


def test_grounded_question_thread_creates_continue_goal_without_llm() -> None:
    event = _event(AgentEventKind.CHAT_RECEIVED, "agent:chat:chat-1", "Kể tiếp đi?")
    thread = OpenThread(
        thread_id="agent:chat:chat-1", topic="cà phê", summary="Kể tiếp đi?",
        created_at=NOW, updated_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    goal = _policy().candidates_for(
        event, AgentStateSnapshot(open_threads=(thread,)), GoalSnapshot(),
    )[0]
    assert goal.kind is GoalKind.CONTINUE_THREAD
    assert goal.parent_thread_id == thread.thread_id
    assert goal.source is GoalSource.RULE


def test_unrelated_event_creates_nothing() -> None:
    event = _event(AgentEventKind.EMOTION_APPLIED, "emo-1", "")
    assert _policy().candidates_for(event, AgentStateSnapshot(), GoalSnapshot()) == ()


def test_agenda_does_not_apply_mood_priority_to_created_goal() -> None:
    kinds = list(GoalKind)
    mood_policy = MoodActionPolicy(MoodPolicyConfig(
        6, 0, 100, 50,
        {"bon_chon": {"continue_thread": 10}}, {}, {},
    ))
    policy = AgendaPolicy(
        AgendaPolicyConfig(
            priorities={kind: 40 for kind in kinds},
            ttl_seconds={kind: 60 for kind in kinds},
        ),
        clock=lambda: NOW,
        mood_policy=mood_policy,
    )
    event = _event(AgentEventKind.CHAT_RECEIVED, "agent:chat:mood", "Kể tiếp đi?")
    thread = OpenThread(
        "agent:chat:mood", "story", "unfinished story", NOW, NOW,
        NOW + timedelta(minutes=5),
    )
    goal = policy.candidates_for(
        event, AgentStateSnapshot(open_threads=(thread,)), GoalSnapshot(),
        mood=MoodState(bon_chon=8),
    )[0]
    assert goal.priority == 40


@pytest.mark.parametrize("value", [True, "60", 2.5, 0, -1])
def test_agenda_policy_config_rejects_coerced_or_invalid_ttl(value: object) -> None:
    with pytest.raises(ValueError):
        AgendaPolicyConfig(
            priorities={kind: 40 for kind in GoalKind},
            ttl_seconds={
                kind: (value if kind is GoalKind.CONTINUE_THREAD else 60)
                for kind in GoalKind
            },
        )


def test_agenda_policy_config_requires_complete_kind_inventory() -> None:
    with pytest.raises(ValueError, match="every GoalKind"):
        AgendaPolicyConfig(
            priorities={GoalKind.CONTINUE_THREAD: 40},
            ttl_seconds={kind: 60 for kind in GoalKind},
        )
