from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, Segment
from services.director.salience import SaliencePool


def _director() -> Director:
    director = Director(
        SaliencePool(base_tier={"chat": 10}, floor=1),
        ChatPulse(),
        [Segment("main", "main", 300, {"read_chat", "self_talk"})],
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
