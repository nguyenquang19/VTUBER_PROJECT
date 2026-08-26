"""Executable payload contracts for strict Director V2 primary ownership."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from interfaces.director_v2 import DirectorV2Proposal
from interfaces.state import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
    ShortIntention,
    ShortIntentionStatus,
)
from interfaces.state import AgentStateSnapshot, OpenThread
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, ReadMode, Segment
from services.director.salience import SaliencePool
from services.director.v2_primary import (
    DirectorV2DecisionMaterializer,
    DirectorV2MaterializationError,
)


def _director() -> Director:
    value = Director(
        SaliencePool(base_tier={"chat": 10}, floor=1),
        ChatPulse(),
        [Segment("main", "main", 300, {
            "read_chat", "ack_donation", "self_talk", "follow_up",
            "continue_thread", "ask_follow_up", "share_goal_progress",
        })],
        ask_follow_up_before_expiry_s=20,
    )
    value.start(0.0)
    return value


def _proposal(
    action: str, candidate: str, *, evidence: tuple[str, ...] = (),
) -> DirectorV2Proposal:
    return DirectorV2Proposal(
        f"p:{action}:{candidate}", 1.0, action, action, candidate,
        ("selected", "validated"), evidence,
    )


def _input(**changes: object) -> DirectorInput:
    values: dict[str, object] = {
        "now": 1.0,
        "agent_state": AgentStateSnapshot(),
        "goals": GoalSnapshot(),
    }
    values.update(changes)
    return DirectorInput(**values)  # type: ignore[arg-type]


def _goal(kind: GoalKind, **changes: object) -> tuple[Goal, ShortIntention]:
    created = datetime.fromtimestamp(1.0, timezone.utc)
    values: dict[str, object] = {
        "goal_id": f"goal:{kind.value}",
        "kind": kind,
        "status": GoalStatus.ACTIVE,
        "priority": 50,
        "reason": "grounded goal",
        "source": GoalSource.RULE,
        "created_at": created,
        "expires_at": created + timedelta(seconds=60),
        "success_conditions": ("speech_completed",),
    }
    values.update(changes)
    goal = Goal(**values)  # type: ignore[arg-type]
    intention = ShortIntention(
        f"intention:{goal.goal_id}", goal.goal_id, ShortIntentionStatus.ACTIVE,
        0, 1, "complete grounded step", created, created,
        goal.expires_at, "activated",
    )
    return goal, intention


def test_materializes_wait_chat_donation_and_self_talk_deterministically() -> None:
    materializer = DirectorV2DecisionMaterializer(_director())
    chat = DirectorChatRef("chat-1", "hello", "chat", 10, 0.0)
    donation = replace(chat, msg_id="donation-1", is_super=True, amount_vnd=10000)
    value = _input(
        chat_candidates=(chat, donation), urge_ready=True, self_talk_ready=True,
    )

    wait = materializer.materialize(_proposal("WAIT", "wait"), value)
    read = materializer.materialize(_proposal("READ_CHAT", "chat-1"), value)
    ack = materializer.materialize(_proposal("READ_CHAT", "donation-1"), value)
    talk = materializer.materialize(
        _proposal("SELF_TALK", "urge", evidence=("proactive:urge",)), value,
    )

    assert wait.action is DirectorAction.WAIT
    assert read.action is DirectorAction.READ_CHAT and read.read_mode is ReadMode.SINGLE
    assert read.refs == (chat,)
    assert ack.action is DirectorAction.ACK_DONATION and ack.read_mode is ReadMode.ACK
    assert talk.action is DirectorAction.SELF_TALK
    assert all(
        item.decision_owner == "director_v2" and item.director_v2_proposal_id
        for item in (wait, read, ack, talk)
    )
    assert materializer.materialize(_proposal("READ_CHAT", "chat-1"), value) == read


def test_self_talk_materialization_revalidates_director_cooldown() -> None:
    director = _director()
    director.mark_spoke(DirectorAction.SELF_TALK, 0.0)

    with pytest.raises(DirectorV2MaterializationError, match="self_talk_not_ready"):
        DirectorV2DecisionMaterializer(director).materialize(
            _proposal("SELF_TALK", "urge", evidence=("proactive:urge",)),
            _input(urge_ready=True, self_talk_ready=True),
        )


@pytest.mark.parametrize(
    ("kind", "goal_changes", "expected"),
    [
        (GoalKind.CONTINUE_THREAD, {"parent_thread_id": "thread-1"}, DirectorAction.CONTINUE_THREAD),
        (
            GoalKind.WAIT_FOR_CHAT_ANSWER,
            {"expires_at": datetime.fromtimestamp(10.0, timezone.utc)},
            DirectorAction.ASK_FOLLOW_UP,
        ),
        (GoalKind.OPERATOR_PINNED, {}, DirectorAction.SHARE_GOAL_PROGRESS),
    ],
)
def test_materializes_grounded_goal_aliases(
    kind: GoalKind, goal_changes: dict[str, object], expected: DirectorAction,
) -> None:
    now = datetime.fromtimestamp(1.0, timezone.utc)
    thread = OpenThread(
        "thread-1", "topic", "grounded summary", now, now,
        now + timedelta(minutes=5),
    )
    goal, intention = _goal(kind, **goal_changes)
    value = _input(
        agent_state=AgentStateSnapshot(open_threads=(thread,)),
        goals=GoalSnapshot(active=goal, current_intention=intention,
                           intentions=(intention,)),
    )
    decision = DirectorV2DecisionMaterializer(_director()).materialize(
        _proposal("FOLLOW_UP", goal.goal_id), value,
    )
    assert decision.action is expected
    assert decision.goal_id == goal.goal_id


def test_materializes_answer_goal_and_open_thread_from_same_tick_evidence() -> None:
    now = datetime.fromtimestamp(1.0, timezone.utc)
    ref = DirectorChatRef("answer-1", "the answer", "chat", 10, 0.0)
    goal, intention = _goal(
        GoalKind.ANSWER_FOLLOW_UP,
        metadata={"chat_event_id": "agent:chat:answer-1"},
    )
    thread = OpenThread(
        "thread-1", "topic", "grounded summary", now, now,
        now + timedelta(minutes=5),
    )
    materializer = DirectorV2DecisionMaterializer(_director())
    answer = materializer.materialize(
        _proposal("FOLLOW_UP", goal.goal_id),
        _input(
            chat_candidates=(ref,),
            goals=GoalSnapshot(active=goal, current_intention=intention,
                               intentions=(intention,)),
        ),
    )
    follow = materializer.materialize(
        _proposal("FOLLOW_UP", thread.thread_id),
        _input(agent_state=AgentStateSnapshot(open_threads=(thread,))),
    )
    assert answer.action is DirectorAction.READ_CHAT and answer.refs == (ref,)
    assert follow.action is DirectorAction.FOLLOW_UP
    assert follow.proactive_source_id == thread.thread_id
    assert follow.proactive_summary == thread.summary


@pytest.mark.parametrize(
    "proposal,value",
    [
        (_proposal("READ_CHAT", "missing"), _input()),
        (_proposal("SELF_TALK", "urge"), _input(urge_ready=False)),
        (_proposal("FOLLOW_UP", "missing"), _input()),
    ],
)
def test_materialization_rejects_missing_or_unready_evidence(
    proposal: DirectorV2Proposal, value: DirectorInput,
) -> None:
    with pytest.raises(DirectorV2MaterializationError):
        DirectorV2DecisionMaterializer(_director()).materialize(proposal, value)
