"""Render a small, relevant set of grounded state items for prompts (M1.5)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.state import AgentEventKind, AgentStateSnapshot, GroundedEvent

_WORD_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)
_KIND_WEIGHT = {
    AgentEventKind.DONATION_RECEIVED: 5.0,
    AgentEventKind.CHAT_RECEIVED: 4.0,
    AgentEventKind.SPEECH_FINAL: 3.5,
    AgentEventKind.SELF_TALK_COMPLETED: 3.0,
    AgentEventKind.DIRECTOR_ACTION: 2.0,
    AgentEventKind.EMOTION_APPLIED: 1.0,
    AgentEventKind.ENVIRONMENT_OBSERVED: 0.5,
}


@dataclass(frozen=True)
class ContextRenderConfig:
    min_items: int
    max_items: int
    item_max_chars: int
    relevance_window_s: float

    @classmethod
    def from_loader(cls, loader: Any) -> "ContextRenderConfig":
        get = lambda key: loader.get("agent_state", f"context.{key}")  # noqa: E731
        config = cls(
            min_items=int(get("min_items")),
            max_items=int(get("max_items")),
            item_max_chars=int(get("item_max_chars")),
            relevance_window_s=float(get("relevance_window_s")),
        )
        if not 1 <= config.min_items <= config.max_items:
            raise ValueError("context item limits must satisfy 1 <= min <= max")
        if min(config.item_max_chars, config.relevance_window_s) <= 0:
            raise ValueError("context renderer limits must be positive")
        return config


class AgentContextRenderer:
    def __init__(
        self,
        config: ContextRenderConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_loader(
        cls, loader: Any, clock: Callable[[], datetime] | None = None,
    ) -> "AgentContextRenderer":
        return cls(ContextRenderConfig.from_loader(loader), clock)

    def render(self, snapshot: AgentStateSnapshot, query: str = "") -> str | None:
        now = _utc(self._clock())
        cutoff = now - timedelta(seconds=self.config.relevance_window_s)
        candidates = [event for event in snapshot.recent_events if event.timestamp >= cutoff]
        if len(candidates) < self.config.min_items:
            return None

        query_terms = _terms(query)
        topic_terms = _terms(snapshot.current_topic.summary if snapshot.current_topic else "")
        ranked = sorted(
            candidates,
            key=lambda event: self._score(event, query_terms, topic_terms, now),
            reverse=True,
        )[: self.config.max_items]
        if len(ranked) < self.config.min_items:
            return None
        ranked.sort(key=lambda event: (event.timestamp, event.event_id))
        lines = [
            "[Grounded working context — facts from recorded events only; do not infer missing facts]"
        ]
        lines.extend(self._render_event(event) for event in ranked)
        return "\n".join(lines)

    def _score(
        self,
        event: GroundedEvent,
        query_terms: set[str],
        topic_terms: set[str],
        now: datetime,
    ) -> tuple[float, float, str]:
        payload_terms = _terms(" ".join(_payload_text(event)))
        overlap = len(payload_terms & query_terms) * 3 + len(payload_terms & topic_terms) * 2
        age_s = max(0.0, (now - event.timestamp).total_seconds())
        recency = max(0.0, 1.0 - age_s / self.config.relevance_window_s)
        score = _KIND_WEIGHT.get(event.kind, 0.0) + overlap + recency
        return score, event.timestamp.timestamp(), event.event_id

    def _render_event(self, event: GroundedEvent) -> str:
        payload = event.payload
        if event.kind in (
            AgentEventKind.CHAT_RECEIVED,
            AgentEventKind.DONATION_RECEIVED,
            AgentEventKind.SPEECH_FINAL,
            AgentEventKind.SELF_TALK_COMPLETED,
        ):
            detail = str(payload.get("text") or "")
        elif event.kind is AgentEventKind.EMOTION_APPLIED:
            detail = f"category={payload.get('category')}"
        elif event.kind is AgentEventKind.DIRECTOR_ACTION:
            detail = (
                f"action={payload.get('action')}; stream_phase={payload.get('stream_phase')}"
            )
        else:
            detail = "; ".join(
                f"{key}={value}" for key, value in sorted(payload.items())
            )
        detail = _compact(detail, self.config.item_max_chars)
        source_id = event.provenance.source_event_id or event.event_id
        return (
            f"- {event.kind.value} [{event.source.value}; "
            f"producer={event.provenance.producer}; source_id={source_id}]: {detail}"
        )


def _payload_text(event: GroundedEvent) -> list[str]:
    values: list[str] = []
    for key in ("text", "category", "action", "stream_phase", "emotion_category"):
        value = event.payload.get(key)
        if value is not None:
            values.append(str(value))
    return values


def _terms(value: str) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(value) if len(word) > 1}


def _compact(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
