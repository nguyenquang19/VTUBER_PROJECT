"""Deterministic pseudonymous viewer profile manager (M7.1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from interfaces.base import HealthStatus
from interfaces.relationship import RelationshipService
from services.data.sanitize import hash_viewer_id
from services.data.sanitize import mask_pii
from services.relationship.store import RelationshipStore
from services.relationship.types import (
    RelationshipNote,
    RelationshipSnapshot,
    ReviewStatus,
    ViewerProfile,
)


@dataclass(frozen=True)
class RelationshipLimits:
    profile_ttl_days: int = 30
    seen_event_ttl_days: int = 30
    max_profiles_snapshot: int = 100
    max_preferences: int = 5
    max_boundaries: int = 5
    max_text_chars: int = 240
    note_ttl_days: int = 30
    max_notes_per_viewer: int = 20

    @classmethod
    def from_loader(cls, loader: Any) -> "RelationshipLimits":
        prefix = "relationships."
        value = cls(
            profile_ttl_days=int(loader.get("relationships", prefix + "profile_ttl_days", 30)),
            seen_event_ttl_days=int(loader.get("relationships", prefix + "seen_event_ttl_days", 30)),
            max_profiles_snapshot=int(loader.get("relationships", prefix + "max_profiles_snapshot", 100)),
            max_preferences=int(loader.get("relationships", prefix + "max_preferences", 5)),
            max_boundaries=int(loader.get("relationships", prefix + "max_boundaries", 5)),
            max_text_chars=int(loader.get("relationships", prefix + "max_text_chars", 240)),
            note_ttl_days=int(loader.get("relationships", "notes.ttl_days", 30)),
            max_notes_per_viewer=int(loader.get("relationships", "notes.max_per_viewer", 20)),
        )
        if min(
            value.profile_ttl_days, value.seen_event_ttl_days, value.max_profiles_snapshot,
            value.max_preferences, value.max_boundaries, value.max_text_chars,
            value.note_ttl_days, value.max_notes_per_viewer,
        ) <= 0:
            raise ValueError("relationship limits must be positive")
        return value


class RelationshipManager(RelationshipService):
    service_id = "relationship_manager"

    def __init__(
        self, store: RelationshipStore, limits: RelationshipLimits, *,
        metrics: Any = None, clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
        evidence_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._store = store
        self.limits = limits
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._enabled = bool(enabled)
        self._evidence_exists = evidence_exists or (lambda _event_id: False)
        self._running = False
        self._accepted = 0
        self._duplicates = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, store: RelationshipStore | None = None, metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
        evidence_exists: Callable[[str], bool] | None = None,
    ) -> "RelationshipManager":
        return cls(
            store or RelationshipStore(loader.get("system", "paths.db_file", "data/mai.db")),
            RelationshipLimits.from_loader(loader), metrics=metrics, clock=clock,
            enabled=enabled,
            evidence_exists=evidence_exists,
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
        notes = tuple(item for item in self._store.list_notes() if item.expires_at > now)
        return RelationshipSnapshot(profiles=profiles, notes=notes)

    def update_profile(
        self, viewer_id: str, *, preferences: list[str], boundaries: list[str],
        tone: str | None, evidence_refs: list[str], reason: str,
    ) -> ViewerProfile | None:
        if not self._enabled or self.get_profile(viewer_id) is None:
            return None
        if not self._valid_evidence(evidence_refs) or not reason.strip():
            self._record("rejected", "profile_missing_evidence")
            return None
        prefs = self._clean_values(preferences, self.limits.max_preferences)
        bounds = self._clean_values(boundaries, self.limits.max_boundaries)
        clean_tone = self._clean(tone) if tone else None
        profile = self._store.update_profile_attributes(
            viewer_id, preferences=prefs, boundaries=bounds,
            tone=clean_tone, updated_at=_utc(self._clock()),
        )
        self._audit("profile_update", viewer_id, viewer_id, reason)
        self._record("updated", "profile")
        return profile

    def create_note(
        self, viewer_id: str, *, summary: str, evidence_refs: list[str], reason: str,
    ) -> RelationshipNote | None:
        clean = self._clean(summary)
        if (
            not self._enabled or self.get_profile(viewer_id) is None or not clean
            or not reason.strip() or not self._valid_evidence(evidence_refs)
        ):
            self._record("rejected", "note_validation")
            return None
        if len(self._store.list_notes(viewer_id)) >= self.limits.max_notes_per_viewer:
            self._record("rejected", "note_cap")
            return None
        now = _utc(self._clock())
        note = RelationshipNote(
            note_id=f"note:{uuid.uuid4().hex}", viewer_id=viewer_id,
            summary=clean, evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            status=ReviewStatus.PENDING, source="operator", created_at=now,
            expires_at=now + timedelta(days=self.limits.note_ttl_days),
        )
        self._store.insert_note(note)
        self._audit("note_create", viewer_id, note.note_id, reason)
        self._record("created", "note_pending")
        return note

    def review_note(self, note_id: str, *, approve: bool, reason: str) -> bool:
        note = self._store.get_note(note_id)
        if note is None or not reason.strip():
            return False
        status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        ok = self._store.review_note(note_id, status, _utc(self._clock()))
        if ok:
            self._audit("note_review", note.viewer_id, note_id, reason)
            self._record("reviewed", status.value)
        return ok

    def delete_note(self, note_id: str, *, reason: str) -> bool:
        note = self._store.get_note(note_id)
        if note is None or not reason.strip():
            return False
        ok = self._store.delete_note(note_id)
        if ok:
            self._audit("note_delete", note.viewer_id, note_id, reason)
            self._record("deleted", "note")
        return ok

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def _valid_evidence(self, refs: list[str]) -> bool:
        unique = tuple(dict.fromkeys(str(item).strip() for item in refs if str(item).strip()))
        return bool(unique) and all(self._evidence_exists(item) for item in unique)

    def _clean_values(self, values: list[str], cap: int) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item for item in (self._clean(v) for v in values) if item))[:cap]

    def _clean(self, value: str | None) -> str:
        return " ".join((mask_pii(value) or "").split())[: self.limits.max_text_chars]

    def _audit(self, action: str, viewer_id: str | None, target_id: str, reason: str) -> None:
        self._store.write_audit(
            audit_id=f"audit:{uuid.uuid4().hex}", action=action, viewer_id=viewer_id,
            target_id=target_id, reason=self._clean(reason), created_at=_utc(self._clock()),
        )

    def _record(self, outcome: str, reason: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_relationship_event"):
            self._metrics.record_relationship_event(outcome, reason)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
