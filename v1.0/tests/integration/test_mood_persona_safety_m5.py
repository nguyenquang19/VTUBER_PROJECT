from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from interfaces.animation import MoodState
from orchestrator.config_loader import ConfigLoader
from services.agent.behavior_library import BehaviorLibrary
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.director_loop import DirectorLoop
from services.director.salience import SaliencePool

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Parsed:
    text: str = "safe reply"
    ok: bool = True


class _Runner:
    session_id = "m5-safety"

    def __init__(self) -> None:
        self.stages: list[str] = []

    async def run_turn(self, **kwargs):
        self.stages.append(kwargs.get("stage_direction") or "")
        return _Parsed(), 0


class _Emotion:
    def __init__(self, flag: str) -> None:
        self.flag = flag

    def current_mood(self):
        return MoodState(buc=10)

    def active_tone_flags(self):
        return {self.flag}


@pytest.mark.parametrize(
    ("flag", "behavior", "required", "forbidden"),
    [
        ("force_gentle_tone", "acknowledge", "do not roast", "tease lightly"),
        ("force_deflect", "deflect", "do not flirt back", "tease lightly"),
    ],
)
async def test_safety_flag_wins_strong_roast_persona(
    flag: str, behavior: str, required: str, forbidden: str,
) -> None:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    library = BehaviorLibrary.from_loader(loader)
    pool = SaliencePool(base_tier={"chat": 10}, floor=1)
    pulse = ChatPulse()
    director = Director(
        pool, pulse, [Segment("main", "main", 300, {"read_chat"})],
    )
    director.start(0.0)
    pool.add("chat-1", "grounded viewer message", 0.0, kind="chat")
    runner = _Runner()
    loop = DirectorLoop(
        director, pool, pulse, runner, emotion=_Emotion(flag),
        behavior_library=library, clock=lambda: 1.0,
    )
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    stage = runner.stages[0].lower()
    assert f"behavior [{behavior}]" in stage
    assert required in stage
    assert forbidden not in stage
