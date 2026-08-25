"""Bounded in-memory grounded event ledger (Master Plan M1.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.agent import EventLedgerService
from interfaces.base import HealthStatus
from interfaces.state import GroundedEvent


class EventLedger(EventLedgerService):
    service_id = "event_ledger"

    def __init__(
        self,
        max_events: int,
        event_ttl_s: float,
        dedup_ttl_s: float,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
    ) -> None:
        if min(max_events, event_ttl_s, dedup_ttl_s) <= 0:
            raise ValueError("ledger limits must be positive")
        self._max_events = int(max_events)
        self._event_ttl_s = float(event_ttl_s)
        self._dedup_ttl_s = float(dedup_ttl_s)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._events: list[GroundedEvent] = []
        self._seen: dict[str, datetime] = {}
        self._running = False
        self._accepted = 0
        self._dropped: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
    ) -> "EventLedger":
        return cls(
            max_events=int(loader.get("agent_state", "agent_state.recent_events_max")),
            event_ttl_s=float(loader.get("agent_state", "agent_state.recent_event_ttl_s")),
            dedup_ttl_s=float(loader.get("agent_state", "agent_state.dedup_ttl_s")),
            clock=clock,
            metrics=metrics,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, retained=len(self._events))

    def append(self, event: GroundedEvent) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        if event.event_id in self._seen:
            self._drop("duplicate")
            return False
        if event.timestamp < now - timedelta(seconds=self._event_ttl_s):
            self._seen[event.event_id] = now
            self._drop("expired")
            return False

        candidate = sorted(
            (*self._events, event), key=lambda item: (item.timestamp, item.event_id),
        )
        if len(candidate) > self._max_events and candidate[0].event_id == event.event_id:
            self._seen[event.event_id] = now
            self._drop("capacity")
            return False
        if len(candidate) > self._max_events:
            candidate = candidate[-self._max_events:]
            self._drop("capacity")
        self._events = list(candidate)
        self._seen[event.event_id] = now
        self._accepted += 1
        self._record_metric("accepted", "accepted")
        return True

    def recent(
        self, limit: int | None = None, *, now: datetime | None = None,
    ) -> tuple[GroundedEvent, ...]:
        self._prune(_utc(now or self._clock()))
        events = self._events if limit is None else self._events[-max(0, int(limit)):]
        return tuple(events)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "agent_events_accepted_total": self._accepted,
            "agent_events_dropped_total": sum(self._dropped.values()),
            "agent_events_dropped_by_reason": dict(self._dropped),
            "agent_events_retained": len(self._events),
        }

    def _prune(self, now: datetime) -> None:
        event_cutoff = now - timedelta(seconds=self._event_ttl_s)
        self._events = [event for event in self._events if event.timestamp >= event_cutoff]
        seen_cutoff = now - timedelta(seconds=self._dedup_ttl_s)
        self._seen = {
            event_id: seen_at for event_id, seen_at in self._seen.items()
            if seen_at >= seen_cutoff
        }

    def _drop(self, reason: str) -> None:
        self._dropped[reason] = self._dropped.get(reason, 0) + 1
        self._record_metric("dropped", reason)

    def _record_metric(self, outcome: str, reason: str) -> None:
        if self._metrics is None or not hasattr(self._metrics, "record_agent_event"):
            return
        try:
            self._metrics.record_agent_event(outcome, reason)
        except Exception:
            pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
