"""Contract for grounded, cause-first self-talk planning."""
from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interfaces.animation import MoodState
from interfaces.base import Service


class SelfTalkStage(str, Enum):
    OPEN = "open"
    DEVELOP = "develop"
    INVITE = "invite"
    WAIT = "wait"
    GROUNDED = "grounded"


class ThoughtCause(str, Enum):
    """Observable reason a thought exists; never a fabricated narrative premise."""

    GROUNDED = "grounded"
    RECENT_CONTEXT = "recent_context"
    ENVIRONMENT = "environment"
    SILENCE = "silence"


class SelfTalkContext(BaseModel):
    """Bounded state the planner may use to form a thought."""

    model_config = ConfigDict(frozen=True)

    silence_seconds: float = Field(default=0.0, ge=0.0)
    chat_count_recent: int = Field(default=0, ge=0)
    recent_context: tuple[str, ...] = ()
    environment_summary: str | None = None


class SelfTalkPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    thought_id: str
    cause: ThoughtCause
    intention: str
    evidence_refs: tuple[str, ...] = ()
    grounding_text: str | None = None
    stage: SelfTalkStage
    prompt_text: str
    one_shot: bool = False
    max_sentences: int = Field(default=2, ge=1, le=4)
    allow_question: bool = False


class SelfTalkValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    reasons: tuple[str, ...] = ()


class SelfTalkReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    reason: str = "ready"
    retry_at: float | None = None


class SelfTalkPlanningService(Service):
    """Plan content; the Director remains the only authority that decides when to speak."""

    @abstractmethod
    def prepare(
        self,
        *,
        mood: MoodState,
        now: float,
        base_prompt: str | None = None,
        category: str | None = None,
        tone_flags: tuple[str, ...] = (),
        context: SelfTalkContext | None = None,
    ) -> SelfTalkPlan | None:
        """Reserve a plan without advancing durable arc state."""

    @abstractmethod
    def validate_output(self, plan_id: str, text: str) -> SelfTalkValidation:
        """Validate bounded natural-speech shape before the delivery boundary."""

    @abstractmethod
    def can_deliver(self, plan_id: str) -> bool:
        """Return false when new activity invalidated a pending ambient output."""

    @abstractmethod
    def readiness(self, now: float) -> SelfTalkReadiness:
        """Expose non-mutating scheduling readiness to Director."""

    @abstractmethod
    def defer_until(self, retry_at: float) -> None:
        """Block planning/readiness until an absolute retry deadline."""

    @abstractmethod
    def commit(self, plan_id: str, delivered_text: str, now: float) -> bool:
        """Advance state only after the delivery boundary confirms success."""

    @abstractmethod
    def release(self, plan_id: str) -> None:
        """Release a failed plan without advancing its arc."""

    @abstractmethod
    def on_chat(self, now: float) -> None:
        """Suspend an arc or resolve its invitation when real chat arrives."""

    @abstractmethod
    def set_enabled(self, enabled: bool) -> None:
        """Apply the runtime feature toggle."""

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether planning is enabled."""

    @property
    @abstractmethod
    def unavailable_retry_seconds(self) -> float:
        """How long Director should wait after the planner has no fresh thought."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded operator-visible state."""
