from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from interfaces.animation import MoodState
from orchestrator.config_loader import ConfigLoader
from interfaces.state import GoalKind
from services.agent.mood_policy import MoodActionPolicy
from services.director.action_types import DirectorChatRef
from services.director.director import DirectorAction
from tests.unit.test_director_arbiter import _director, _goal, _input


ROOT = Path(__file__).resolve().parents[2]


def test_extreme_mood_cannot_change_any_goal_priority() -> None:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    policy = MoodActionPolicy.from_loader(loader)
    for kind in GoalKind:
        assert policy.goal_priority(kind, 50, MoodState(bon_chon=10), ()) == 50
        assert policy.goal_priority(kind, 50, MoodState(buc=10), {"force_deflect"}) == 50


def test_safety_and_donation_hard_arbitration_ignore_extreme_mood() -> None:
    donation = replace(_input().chat_candidates[0], is_super=True)
    safety = replace(
        _input(), mood=MoodState(vui=10), safety_hold=True,
        chat_candidates=(donation,),
    )
    assert _director().decide(safety).action is DirectorAction.WAIT
    donation_input = replace(
        _input(), mood=MoodState(buc=10),
        goals=replace(_input().goals, active=_goal(GoalKind.OPERATOR_PINNED)),
        chat_candidates=(DirectorChatRef("d1", "don", "chat", 1, 0, is_super=True),),
    )
    assert _director().decide(donation_input).action is DirectorAction.ACK_DONATION


def test_production_defaults_keep_shadow_and_hybrid_prompt_on() -> None:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    assert loader.get("features", "features.mood_v2_shadow.enabled") is True
    assert loader.get("features", "features.mood_v2_prompt.enabled") is True
