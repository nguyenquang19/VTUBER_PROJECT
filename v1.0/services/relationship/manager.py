"""Deterministic pseudonymous viewer profile manager (M7.1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.relationship import RelationshipService
from services.data.sanitize import hash_viewer_id
from services.relationship.store import RelationshipStore
from services.relationship.types import RelationshipSnapshot, ViewerProfile


@dataclass(frozen=True)
class RelationshipLimits:
    profile_ttl_days: int = 30
    seen_event_ttl_days: int = 30
    max_profiles_snapshot: int = 100

    @classmethod
    def from_loader(cls, loader: Any) -> "RelationshipLimits":
        prefix = "relationships."
        value = cls(
            profile_ttl_days=int(loader.get("relationships", prefix + "profile_ttl_days", 30)),
            seen_event_ttl_days=int(loader.get("relationships", prefix + "seen_event_ttl_days", 30)),
            max_profiles_snapshot=int(loader.get("relationships", prefix + "max_profiles_snapshot", 100)),
        )
        if min(value.profile_ttl_days, value.seen_event_ttl_days, value.max_profiles_snapshot) <= 0:
            raise ValueError("relationship limits must be positive")
        return value


class RelationshipManager(RelationshipService):
    service_id = "relationship_manager"

    def __init__(
        self, store: RelationshipStore, limits: RelationshipLimits, *,
        metrics: Any = None, clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self.limits = limits
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._enabled = bool(enabled)
        self._running = False
        self._accepted = 0
        self._duplicates = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, store: RelationshipStore | None = None, metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
    ) -> "RelationshipManager":
        return cls(
            store or RelationshipStore(loader.get("system", "paths.db_file", "data/mai.db")),
            RelationshipLimits.from_loader(loader), metrics=metrics, clock=clock,
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._store.close()

    async def health_check(self) -> HealthStatus:
        return (
            HealthStatus.healthy(self.service_id, profiles=len(self.snapshot().profiles))
            if self._running else HealthStatus.stopped(self.service_id)
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "relationship_interactions_total": self._accepted,
            "relationship_duplicates_total": self._duplicates,
            "relationship_profiles": len(self.snapshot().profiles),
            "relationship_enabled": self._enabled,
        }

    def observe_interaction(
        self, *, raw_viewer_id: str | None, event_id: str, occurred_at: datetime,
    ) -> ViewerProfile | None:
        if not self._enabled:
            self._record("dropped", "feature_disabled")
            return None
        viewer_id = hash_viewer_id(raw_viewer_id)
        if viewer_id is None or not event_id.strip():
            self._record("dropped", "missing_identity")
            return None
        occurred_at = _utc(occurred_at)
        profile, inserted = self._store.observe_profile(
            viewer_id=viewer_id, event_id=event_id, occurred_at=occurred_at,
            expires_at=occurred_at + timedelta(days=self.limits.profile_ttl_days),
        )
        if inserted:
            self._accepted += 1
            self._record("accepted", "interaction")
            self._store.prune_seen_events(
                self._clock() - timedelta(days=self.limits.seen_event_ttl_days)
            )
        else:
            self._duplicates += 1
            self._record("dropped", "duplicate_event")
        return profile

    def get_profile(self, viewer_id: str) -> ViewerProfile | None:
        profile = self._store.get_profile(viewer_id)
        if profile is None or profile.expires_at <= _utc(self._clock()):
            return None
        return profile

    def snapshot(self) -> RelationshipSnapshot:
        now = _utc(self._clock())
        profiles = tuple(
            item for item in self._store.list_profiles() if item.expires_at > now
        )[: self.limits.max_profiles_snapshot]
        return RelationshipSnapshot(profiles=profiles)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def _record(self, outcome: str, reason: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_relationship_event"):
            self._metrics.record_relationship_event(outcome, reason)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
