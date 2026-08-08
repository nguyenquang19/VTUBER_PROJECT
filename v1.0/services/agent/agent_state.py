"""Pure state reducer plus shared AgentState service (Master Plan M1.1/M1.4)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.agent import AgentStateService
from interfaces.base import HealthStatus
from services.agent.types import (
    AgentEventKind,
    AgentStateSnapshot,
    GroundedEvent,
    OpenThread,
    StreamPhase,
    TopicState,
)


@dataclass(frozen=True)
class AgentStateLimits:
    recent_events_max: int
    recent_event_ttl_s: float
    open_threads_max: int
    open_thread_ttl_s: float
    payload_text_max_chars: int

    @classmethod
    def from_loader(cls, loader: Any) -> "AgentStateLimits":
        get = lambda key: loader.get("agent_state", f"agent_state.{key}")  # noqa: E731
        limits = cls(
            recent_events_max=int(get("recent_events_max")),
            recent_event_ttl_s=float(get("recent_event_ttl_s")),
            open_threads_max=int(get("open_threads_max")),
            open_thread_ttl_s=float(get("open_thread_ttl_s")),
            payload_text_max_chars=int(get("payload_text_max_chars")),
        )
        if min(
            limits.recent_events_max,
            limits.recent_event_ttl_s,
            limits.open_threads_max,
            limits.open_thread_ttl_s,
            limits.payload_text_max_chars,
        ) <= 0:
            raise ValueError("agent state limits must be positive")
        return limits


class AgentStateReducer:
    """Deterministic reducer. It derives state only from accepted grounded events."""

    def __init__(self, limits: AgentStateLimits) -> None:
        self.limits = limits

    def reduce(
        self,
        snapshot: AgentStateSnapshot,
        event: GroundedEvent,
        *,
        now: datetime,
    ) -> AgentStateSnapshot:
        now = _utc(now)
        threads = tuple(thread for thread in snapshot.open_threads if thread.expires_at > now)
        recent = tuple(
            item for item in (*snapshot.recent_events, event)
            if item.timestamp >= now - timedelta(seconds=self.limits.recent_event_ttl_s)
        )
        recent = tuple(sorted(recent, key=lambda item: (item.timestamp, item.event_id)))[
            -self.limits.recent_events_max:
        ]
        next_state = replace(snapshot, open_threads=threads, recent_events=recent)

        if event.kind in (AgentEventKind.CHAT_RECEIVED, AgentEventKind.DONATION_RECEIVED):
            next_state = self._reduce_chat(next_state, event, now)
        elif event.kind is AgentEventKind.SPEECH_FINAL:
            next_state = self._reduce_speech(next_state, event)
        elif event.kind is AgentEventKind.ENVIRONMENT_OBSERVED:
            next_state = replace(next_state, environment_summary=event.payload)
        elif event.kind is AgentEventKind.DIRECTOR_ACTION:
            phase = event.payload.get("stream_phase")
            if phase in {item.value for item in StreamPhase}:
                next_state = replace(next_state, stream_phase=StreamPhase(str(phase)))
        return next_state

    def _reduce_chat(
        self, snapshot: AgentStateSnapshot, event: GroundedEvent, now: datetime,
    ) -> AgentStateSnapshot:
        text = _compact(event.payload.get("text"), self.limits.payload_text_max_chars)
        if not text:
            return snapshot
        current = snapshot.current_topic
        is_follow_up = _looks_like_follow_up(text)
        if current is None or (
            event.timestamp >= current.updated_at and not is_follow_up
        ):
            current = TopicState(
                summary=text,
                source_event_id=event.event_id,
                updated_at=event.timestamp,
                confidence=event.confidence,
            )
        elif current is not None and event.timestamp >= current.updated_at and is_follow_up:
            current = replace(
                current,
                source_event_id=event.event_id,
                updated_at=event.timestamp,
                confidence=min(current.confidence, event.confidence),
            )

        threads = list(snapshot.open_threads)
        if "?" in text:
            if is_follow_up and threads:
                previous = threads[-1]
                threads[-1] = replace(
                    previous,
                    updated_at=event.timestamp,
                    expires_at=now + timedelta(seconds=self.limits.open_thread_ttl_s),
                )
            else:
                threads.append(OpenThread(
                    thread_id=event.event_id,
                    topic=current.summary if current else text,
                    summary=text,
                    created_at=event.timestamp,
                    updated_at=event.timestamp,
                    expires_at=now + timedelta(seconds=self.limits.open_thread_ttl_s),
                ))
        return replace(
            snapshot,
            current_topic=current,
            open_threads=tuple(threads[-self.limits.open_threads_max:]),
        )

    def _reduce_speech(
        self, snapshot: AgentStateSnapshot, event: GroundedEvent,
    ) -> AgentStateSnapshot:
        current = snapshot.last_spoken_summary
        latest_ts = None
        for item in reversed(snapshot.recent_events):
            if item.kind is AgentEventKind.SPEECH_FINAL and item.event_id != event.event_id:
                latest_ts = item.timestamp
                break
        if latest_ts is None or event.timestamp >= latest_ts:
            current = _compact(event.payload.get("text"), self.limits.payload_text_max_chars) or current
        return replace(snapshot, last_spoken_summary=current)


class AgentState(AgentStateService):
    """Shared service. EventLedger composition is wired in M1.2."""

    service_id = "agent_state"

    def __init__(
        self,
        reducer: AgentStateReducer,
        ledger: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reducer = reducer
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._snapshot = AgentStateSnapshot()
        self._running = False
        self._reduce_errors = 0

    @classmethod
    def from_loader(
        cls, loader: Any, ledger: Any, clock: Callable[[], datetime] | None = None,
    ) -> "AgentState":
        return cls(AgentStateReducer(AgentStateLimits.from_loader(loader)), ledger, clock)

    async def start(self) -> None:
        await self._ledger.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self._ledger.stop()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, recent_events=len(self._snapshot.recent_events))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "agent_state_reduce_errors_total": self._reduce_errors,
            "agent_state_open_threads": len(self._snapshot.open_threads),
            **self._ledger.get_metrics(),
        }

    def record(self, event: GroundedEvent) -> bool:
        event = replace(
            event,
            payload=_bound_payload(event.payload, self._reducer.limits.payload_text_max_chars),
        )
        if not self._ledger.append(event):
            return False
        try:
            self._snapshot = self._reducer.reduce(self._snapshot, event, now=self._clock())
            return True
        except Exception:
            self._reduce_errors += 1
            return False

    def snapshot(self) -> AgentStateSnapshot:
        now = _utc(self._clock())
        open_threads = tuple(
            thread for thread in self._snapshot.open_threads if thread.expires_at > now
        )
        return AgentStateSnapshot(**{
            "current_topic": self._snapshot.current_topic,
            "open_threads": open_threads,
            "active_goal_ref": self._snapshot.active_goal_ref,
            "recent_events": self._ledger.recent(now=now),
            "environment_summary": self._snapshot.environment_summary,
            "stream_phase": self._snapshot.stream_phase,
            "last_spoken_summary": self._snapshot.last_spoken_summary,
        })

    def set_active_goal_ref(self, goal_id: str | None) -> None:
        self._snapshot = replace(self._snapshot, active_goal_ref=goal_id)


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _bound_payload(value: Any, max_chars: int) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {
            str(key): _bound_payload(item, max_chars)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_bound_payload(item, max_chars) for item in value]
    if isinstance(value, str):
        return _compact(value, max_chars)
    return value


def _looks_like_follow_up(text: str) -> bool:
    lowered = text.casefold().strip()
    markers = (
        "còn ", "thế ", "vậy ", "rồi sao", "cái đó", "chuyện đó", "nó ",
        "ý đó", "tiếp đi", "kể tiếp",
    )
    return len(lowered) <= 120 and any(
        lowered.startswith(marker) or marker in lowered for marker in markers
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
