"""Immutable inputs for the M3 rule-based action arbiter."""
from __future__ import annotations

from dataclasses import dataclass, field

from interfaces.animation import MoodState
from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot


@dataclass(frozen=True)
class DirectorChatRef:
    msg_id: str
    text: str
    kind: str
    score: float
    created_at: float
    viewer_id: str | None = None
    viewer_name: str | None = None
    amount_vnd: int = 0
    is_super: bool = False
    cluster_count: int = 1


@dataclass(frozen=True)
class DirectorInput:
    now: float
    agent_state: AgentStateSnapshot
    goals: GoalSnapshot
    chat_candidates: tuple[DirectorChatRef, ...] = ()
    pool_size: int = 0
    pulse_state: str = "normal"
    urge_ready: bool = False
    safety_hold: bool = False
    mood: MoodState = field(default_factory=MoodState)
    tone_flags: tuple[str, ...] = ()
