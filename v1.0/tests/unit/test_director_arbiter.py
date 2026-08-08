from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.types import AgentStateSnapshot, OpenThread
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from orchestrator.metrics_collector import MetricsCollector
from services.director.salience import SaliencePool


def _director() -> Director:
    director = Director(
        SaliencePool(base_tier={"chat": 10}, floor=1),
        ChatPulse(),
        [Segment("main", "main", 300, {
            "read_chat", "self_talk", "ack_donation", "continue_thread",
            "ask_follow_up", "share_goal_progress",
        })],
    )
    director.start(0.0)
    return director


def _input() -> DirectorInput:
    return DirectorInput(
        now=1.0,
        agent_state=AgentStateSnapshot(),
        goals=GoalSnapshot(),
        chat_candidates=(DirectorChatRef("c1", "hello", "chat", 10, 0.0),),
        pool_size=1,
        pulse_state="normal",
    )


def test_director_input_and_chat_refs_are_frozen() -> None:
    value = _input()
    with pytest.raises(FrozenInstanceError):
        value.now = 2.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        value.chat_candidates[0].text = "mutated"  # type: ignore[misc]


def test_decide_is_deterministic_and_does_not_mutate_input_or_director() -> None:
    director = _director()
    value = _input()
    before = director.get_metrics()
    first = director.decide(value)
    second = director.decide(value)
    assert first == second
    assert director.get_metrics() == before
    assert value.chat_candidates[0].text == "hello"


def test_decision_refs_are_immutable_tuple() -> None:
    decision = _director().decide(_input())
    assert isinstance(decision.refs, tuple)
    with pytest.raises(FrozenInstanceError):
        decision.reason = "changed"  # type: ignore[misc]


def _goal(kind: GoalKind, **over: object) -> Goal:
    now = datetime.fromtimestamp(1.0, tz=timezone.utc)
    values: dict[str, object] = {
        "goal_id": f"goal:{kind.value}",
        "kind": kind,
        "status": GoalStatus.ACTIVE,
        "priority": 50,
        "reason": "grounded test goal",
        "source": GoalSource.RULE,
        "created_at": now,
        "expires_at": now + timedelta(seconds=60),
        "success_conditions": ("speech_completed",),
    }
    values.update(over)
    return Goal(**values)  # type: ignore[arg-type]


def test_safety_hold_wins_over_donation_and_goal() -> None:
    value = replace(
        _input(),
        safety_hold=True,
        goals=GoalSnapshot(active=_goal(GoalKind.OPERATOR_PINNED)),
        chat_candidates=(replace(_input().chat_candidates[0], is_super=True),),
    )
    decision = _director().decide(value)
    assert decision.action is DirectorAction.WAIT
    assert decision.reason == "safety_hold"


def test_donation_wins_over_active_goal_even_when_not_top_candidate() -> None:
    ordinary = _input().chat_candidates[0]
    donation = DirectorChatRef("d1", "donation", "chat", 5, 0.0, is_super=True)
    value = replace(
        _input(), goals=GoalSnapshot(active=_goal(GoalKind.OPERATOR_PINNED)),
        chat_candidates=(ordinary, donation),
    )
    decision = _director().decide(value)
    assert decision.action is DirectorAction.ACK_DONATION
    assert decision.refs == (donation,)


def test_active_continue_thread_blocks_unrelated_chat() -> None:
    now = datetime.fromtimestamp(1.0, tz=timezone.utc)
    thread = OpenThread("thread-1", "game", "unfinished game topic", now, now, now + timedelta(minutes=5))
    goal = _goal(GoalKind.CONTINUE_THREAD, parent_thread_id="thread-1")
    value = replace(
        _input(), agent_state=AgentStateSnapshot(open_threads=(thread,)),
        goals=GoalSnapshot(active=goal),
    )
    decision = _director().decide(value)
    assert decision.action is DirectorAction.CONTINUE_THREAD
    assert decision.goal_id == goal.goal_id


def test_waiting_goal_near_expiry_asks_once_then_waits() -> None:
    goal = _goal(
        GoalKind.WAIT_FOR_CHAT_ANSWER,
        expires_at=datetime.fromtimestamp(10.0, tz=timezone.utc),
    )
    value = replace(_input(), goals=GoalSnapshot(active=goal), chat_candidates=())
    assert _director().decide(value).action is DirectorAction.ASK_FOLLOW_UP
    asked = replace(goal, metadata={"follow_up_asked": True})
    decision = _director().decide(replace(value, goals=GoalSnapshot(active=asked)))
    assert decision.action is DirectorAction.WAIT


def test_open_thread_suppresses_dead_air_self_talk_without_goal() -> None:
    now = datetime.fromtimestamp(1.0, tz=timezone.utc)
    thread = OpenThread("thread-1", "game", "unfinished", now, now, now + timedelta(minutes=5))
    value = replace(
        _input(), now=30.0, chat_candidates=(),
        agent_state=AgentStateSnapshot(open_threads=(thread,)),
    )
    decision = _director().decide(value)
    assert decision.action is DirectorAction.WAIT
    assert decision.reason == "open_thread_blocks_self_talk"


def test_director_action_metric_has_action_and_reason_labels() -> None:
    metrics = MetricsCollector()
    metrics.record_director_action("wait", "safety_hold")
    assert metrics.director_action_snapshot() == {"wait:safety_hold": 1}
    assert b'mai_director_actions_total{action="wait",reason="safety_hold"} 1.0' in metrics.prometheus_text()
