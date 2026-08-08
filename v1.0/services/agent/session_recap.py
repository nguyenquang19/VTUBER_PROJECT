"""Bounded deterministic session recap with event provenance (M4.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.agent import SessionRecapService
from interfaces.base import HealthStatus
from services.agent.types import (
    AgentEventKind, GroundedEvent, SessionRecap, SessionRecapItem,
)

_RECAP_KINDS = {
    AgentEventKind.CHAT_RECEIVED,
    AgentEventKind.DONATION_RECEIVED,
    AgentEventKind.SPEECH_FINAL,
    AgentEventKind.SELF_TALK_COMPLETED,
}


@dataclass(frozen=True)
class SessionRecapLimits:
    max_items: int = 8
    max_chars: int = 900
    item_max_chars: int = 160

    @classmethod
    def from_loader(cls, loader: Any) -> "SessionRecapLimits":
        prefix = "recap."
        value = cls(
            max_items=int(loader.get("conversation", prefix + "max_items", 8)),
            max_chars=int(loader.get("conversation", prefix + "max_chars", 900)),
            item_max_chars=int(
                loader.get("conversation", prefix + "item_max_chars", 160)
            ),
        )
        if min(value.max_items, value.max_chars, value.item_max_chars) <= 0:
            raise ValueError("session recap limits must be positive")
        return value


class SessionRecapManager(SessionRecapService):
    service_id = "session_recap"

    def __init__(self, limits: SessionRecapLimits, *, metrics: Any = None) -> None:
        self.limits = limits
        self._metrics = metrics
        self._items: tuple[SessionRecapItem, ...] = ()
        self._running = False
        self._accepted = 0
        self._evicted = 0

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None) -> "SessionRecapManager":
        return cls(SessionRecapLimits.from_loader(loader), metrics=metrics)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, items=len(self._items))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "session_recap_items": len(self._items),
            "session_recap_chars": self.snapshot().total_chars,
            "session_recap_accepted_total": self._accepted,
            "session_recap_evicted_total": self._evicted,
        }

    def handle_event(self, event: GroundedEvent) -> None:
        if event.kind not in _RECAP_KINDS:
            return
        text = _compact(event.payload.get("text"), self.limits.item_max_chars)
        if not text:
            return
        prefix = "Mai" if event.kind in {
            AgentEventKind.SPEECH_FINAL, AgentEventKind.SELF_TALK_COMPLETED,
        } else "Viewer"
        item = SessionRecapItem(
            source_event_id=event.event_id,
            kind=event.kind,
            summary=f"{prefix}: {text}",
            timestamp=event.timestamp,
            producer=event.provenance.producer,
        )
        before = len(self._items)
        items = (*self._items, item)[-self.limits.max_items:]
        while items and sum(len(value.summary) for value in items) > self.limits.max_chars:
            items = items[1:]
        self._items = tuple(items)
        self._accepted += 1
        self._evicted += max(0, before + 1 - len(self._items))
        self._observe_chars()

    def snapshot(self) -> SessionRecap:
        return SessionRecap(self._items)

    def _observe_chars(self) -> None:
        if self._metrics is not None and hasattr(self._metrics, "set_session_recap_chars"):
            try:
                self._metrics.set_session_recap_chars(self.snapshot().total_chars)
            except Exception:
                pass


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"
