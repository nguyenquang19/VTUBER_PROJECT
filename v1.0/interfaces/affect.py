"""Bounded turn affect and slow session mood contracts (M10.6)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from interfaces.base import Service


class AffectStyle(str, Enum):
    NEUTRAL = "neutral"
    GENTLE = "gentle"
    CELEBRATE = "celebrate"
    TEASE = "tease"
    DEFLECT = "deflect"
    SHARP = "sharp"


class AffectResponseMode(str, Enum):
    NATURAL = "natural"
    QUICK_ACK = "quick_ack"
    SOFT_ACCEPT = "soft_accept"
    PLAYFUL_ACCEPT = "playful_accept"
    PLAYFUL_PUSHBACK = "playful_pushback"
    SPAM_BOUNDARY = "spam_boundary"
    SUPPORTIVE = "supportive"
    QUIET_SUPPORT = "quiet_support"
    PLAYFUL_DEFLECT = "playful_deflect"
    PLAYFUL_BOUNDARY = "playful_boundary"
    GRATITUDE = "gratitude"
    CELEBRATE_GIFT = "celebrate_gift"
    RECOVERY = "recovery"


class TurnAffect(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    style: AffectStyle = AffectStyle.NEUTRAL
    response_mode: AffectResponseMode = AffectResponseMode.NATURAL
    energy: float = Field(0.5, ge=0.0, le=1.0)
    warmth: float = Field(0.5, ge=0.0, le=1.0)
    urgency: float = Field(0.0, ge=0.0, le=1.0)
    cause_ref: str | None = None
    created_turn: int = Field(0, ge=0)
    expires_at_turn: int = Field(1, ge=1)


class SessionMood(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    valence: float = Field(0.0, ge=-1.0, le=1.0)
    arousal: float = Field(0.0, ge=-1.0, le=1.0)
    irritation: float = Field(0.0, ge=-1.0, le=1.0)
    updated_at: float = 0.0


class ResponsePlan(BaseModel):
    """One observable directive plan composed from legacy mood and turn policy."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    category: str = "default"
    style: AffectStyle = AffectStyle.NEUTRAL
    response_mode: AffectResponseMode = AffectResponseMode.NATURAL
    energy: float = Field(0.5, ge=0.0, le=1.0)
    warmth: float = Field(0.5, ge=0.0, le=1.0)
    urgency: float = Field(0.0, ge=0.0, le=1.0)
    tone_source: Literal["turn", "legacy"] = "turn"
    tone_directive: str | None = None
    max_sentences: int | None = Field(None, ge=1, le=3)


class AffectComposer(ABC):
    @abstractmethod
    def compose(
        self,
        category: str,
        affect: TurnAffect | None,
        legacy_mood: Any,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> ResponsePlan | None:
        """Compose exactly one response plan, or fail open with None."""

    @abstractmethod
    def get_metrics(self) -> dict[str, Any]:
        """Return operator-safe composition counters."""


class AffectService(Service):
    @abstractmethod
    def observe(
        self,
        category: str,
        *,
        targets: dict[str, float],
        tone_flag: str | None,
        cause_ref: str | None,
    ) -> TurnAffect:
        """Map one grounded event into bounded turn and session affect."""

    @abstractmethod
    def current_turn_affect(self) -> TurnAffect | None:
        """Return the active affect, respecting turn TTL."""

    @abstractmethod
    def current_session_mood(self) -> SessionMood:
        """Return elapsed-time-decayed slow session mood."""

    @abstractmethod
    def advance_turn(self) -> None:
        """Advance the explicit turn clock used for TTL."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return an operator-safe shadow snapshot."""
