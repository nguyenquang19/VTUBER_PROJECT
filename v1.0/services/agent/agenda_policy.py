"""Grounded rule factories for M2 goals; no planner or LLM calls."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.animation import MoodState

from services.agent.goal_types import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
)
from services.agent.types import AgentEventKind, AgentStateSnapshot, GroundedEvent


@dataclass(frozen=True)
class AgendaPolicyConfig:
    priorities: dict[GoalKind, int]
    ttl_seconds: dict[GoalKind, int]

    @classmethod
    def from_loader(cls, loader: Any) -> "AgendaPolicyConfig":
        priorities = {
            kind: int(loader.get("agent_goals", f"priorities.{kind.value}", default))
            for kind, default in {
                GoalKind.ACK_DONATION: 100,
                GoalKind.WAIT_FOR_CHAT_ANSWER: 60,
                GoalKind.CONTINUE_THREAD: 40,
                GoalKind.ANSWER_FOLLOW_UP: 70,
                GoalKind.OPERATOR_PINNED: 90,
            }.items()
        }
        ttls = {
            GoalKind.ACK_DONATION: int(
                loader.get("agent_goals", "goal_manager.donation_ttl_s", 120)
            ),
            GoalKind.WAIT_FOR_CHAT_ANSWER: int(
                loader.get("agent_goals", "goal_manager.wait_for_answer_ttl_s", 90)
            ),
            GoalKind.CONTINUE_THREAD: int(
                loader.get("agent_goals", "goal_manager.continue_thread_ttl_s", 600)
            ),
            GoalKind.ANSWER_FOLLOW_UP: int(
                loader.get("agent_goals", "goal_manager.follow_up_ttl_s", 180)
            ),
            GoalKind.OPERATOR_PINNED: int(
                loader.get("agent_goals", "goal_manager.operator_pinned_ttl_s", 3600)
            ),
        }
        return cls(priorities=priorities, ttl_seconds=ttls)


class AgendaPolicy:
    """Turn accepted grounded events into deterministic goal candidates."""

    def __init__(
        self,
        config: AgendaPolicyConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        mood_policy: Any = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mood_policy = mood_policy

    @classmethod
    def from_loader(
        cls, loader: Any, *, clock: Callable[[], datetime] | None = None,
        mood_policy: Any = None,
    ) -> "AgendaPolicy":
        return cls(
            AgendaPolicyConfig.from_loader(loader), clock=clock, mood_policy=mood_policy,
        )

    def candidates_for(
        self,
        event: GroundedEvent,
        state: AgentStateSnapshot,
        goals: GoalSnapshot,
        *,
        mood: MoodState | None = None,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> tuple[Goal, ...]:
        if event.kind is AgentEventKind.DONATION_RECEIVED:
            return (self._donation(event, mood, tone_flags),)
        if (
            event.kind is AgentEventKind.SPEECH_FINAL
            and _is_specific_question(event)
        ) or (
            event.kind is AgentEventKind.SPEECH_COMPLETED
            and event.payload.get("expects_chat_answer") is True
            and _is_specific_question(event)
        ):
            return (self._wait_for_answer(event, state, mood, tone_flags),)
        if event.kind is AgentEventKind.CHAT_RECEIVED:
            active = goals.active
            if active is not None and active.kind is GoalKind.WAIT_FOR_CHAT_ANSWER:
                return (self._answer_follow_up(event, active, mood, tone_flags),)
            thread = _thread_for_event(event, state)
            if thread is not None:
                return (self._continue_thread(
                    event, thread.thread_id, thread.summary, mood, tone_flags,
                ),)
        return ()

    def _make(
        self,
        kind: GoalKind,
        event: GroundedEvent,
        *,
        reason: str,
        success: str,
        parent_thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        mood: MoodState | None = None,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> Goal:
        now = _utc(self._clock())
        return Goal(
            goal_id=f"goal:{kind.value}:{event.event_id}",
            kind=kind,
            status=GoalStatus.CANDIDATE,
            priority=self._priority(kind, mood, tone_flags),
            reason=reason,
            source=GoalSource.RULE,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.ttl_seconds[kind]),
            success_conditions=(success,),
            parent_thread_id=parent_thread_id,
            metadata={"source_event_id": event.event_id, "relevant": True, **(metadata or {})},
        )

    def _donation(
        self, event: GroundedEvent, mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...],
    ) -> Goal:
        alias = str(event.payload.get("viewer_alias") or "viewer")
        return self._make(
            GoalKind.ACK_DONATION,
            event,
            reason=f"acknowledge donation from {alias}",
            success=f"speech_completed acknowledges {event.event_id}",
            metadata={
                "viewer_alias": alias,
                "amount_vnd": int(event.payload.get("amount_vnd", 0) or 0),
            },
            mood=mood, tone_flags=tone_flags,
        )

    def _wait_for_answer(
        self, event: GroundedEvent, state: AgentStateSnapshot, mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...],
    ) -> Goal:
        parent = state.open_threads[-1].thread_id if state.open_threads else None
        question = str(event.payload.get("text") or "")
        return self._make(
            GoalKind.WAIT_FOR_CHAT_ANSWER,
            event,
            reason="Mai asked chat a specific grounded question",
            success="a newer chat_received event answers the question",
            parent_thread_id=parent,
            metadata={"question": question},
            mood=mood, tone_flags=tone_flags,
        )

    def _answer_follow_up(
        self, event: GroundedEvent, waiting: Goal, mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...],
    ) -> Goal:
        return self._make(
            GoalKind.ANSWER_FOLLOW_UP,
            event,
            reason="viewer replied while Mai was waiting for chat",
            success=f"speech_completed answers {event.event_id}",
            parent_thread_id=waiting.parent_thread_id,
            metadata={
                "chat_event_id": event.event_id,
                "text": str(event.payload.get("text") or ""),
                "waiting_goal_id": waiting.goal_id,
            },
            mood=mood, tone_flags=tone_flags,
        )

    def _continue_thread(
        self, event: GroundedEvent, thread_id: str, summary: str,
        mood: MoodState | None, tone_flags: set[str] | tuple[str, ...],
    ) -> Goal:
        return self._make(
            GoalKind.CONTINUE_THREAD,
            event,
            reason="continue a grounded open thread",
            success="thread is addressed by speech_completed or operator",
            parent_thread_id=thread_id,
            metadata={"summary": summary},
            mood=mood, tone_flags=tone_flags,
        )

    def _priority(
        self, kind: GoalKind, mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...],
    ) -> int:
        base = self.config.priorities[kind]
        if self._mood_policy is None:
            return base
        try:
            return int(self._mood_policy.goal_priority(kind, base, mood, tone_flags))
        except Exception:
            return base


def _is_specific_question(event: GroundedEvent) -> bool:
    text = " ".join(str(event.payload.get("text") or "").split()).rstrip('"”’)]}')
    return text.endswith("?") and len(text) >= 4


def _thread_for_event(event: GroundedEvent, state: AgentStateSnapshot) -> Any:
    for thread in reversed(state.open_threads):
        evidence_ids = {item.source_event_id for item in thread.evidence}
        if (
            thread.thread_id == event.event_id
            or thread.origin_event_id == event.event_id
            or event.event_id in evidence_ids
            or thread.thread_id in {
                event.event_id.removeprefix("agent:chat:"),
                event.provenance.source_event_id,
            }
        ):
            return thread
    return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
