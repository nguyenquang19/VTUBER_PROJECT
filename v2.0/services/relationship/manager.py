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
from interfaces.relationship import (
    RelationshipNote,
    RelationshipSnapshot,
    NarrativeItem,
    NarrativeStatus,
    ReviewStatus,
    RunningGag,
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
    narrative_ttl_days: int = 30
    max_narratives: int = 50
    prompt_max_items: int = 2
    prompt_max_chars: int = 360
    positive_interactions_required: int = 3
    positive_emotion_categories: tuple[str, ...] = (
        "chat_compliment", "chat_mention_direct", "donation_small", "donation_large",
    )
    gag_reference_cooldown_s: int = 1800
    max_gags_per_viewer: int = 5

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
            narrative_ttl_days=int(loader.get("relationships", "narrative.ttl_days", 30)),
            max_narratives=int(loader.get("relationships", "narrative.max_items", 50)),
            prompt_max_items=int(loader.get("relationships", "narrative.prompt_max_items", 2)),
            prompt_max_chars=int(loader.get("relationships", "narrative.prompt_max_chars", 360)),
            positive_interactions_required=int(loader.get(
                "relationships", "running_gags.positive_interactions_required", 3,
            )),
            positive_emotion_categories=tuple(str(item) for item in loader.get(
                "relationships", "running_gags.positive_emotion_categories",
                ["chat_compliment", "chat_mention_direct", "donation_small", "donation_large"],
            )),
            gag_reference_cooldown_s=int(loader.get(
                "relationships", "running_gags.reference_cooldown_seconds", 1800,
            )),
            max_gags_per_viewer=int(loader.get(
                "relationships", "running_gags.max_per_viewer", 5,
            )),
        )
        if min(
            value.profile_ttl_days, value.seen_event_ttl_days, value.max_profiles_snapshot,
            value.max_preferences, value.max_boundaries, value.max_text_chars,
            value.note_ttl_days, value.max_notes_per_viewer,
            value.narrative_ttl_days, value.max_narratives,
            value.prompt_max_items, value.prompt_max_chars,
            value.positive_interactions_required, value.gag_reference_cooldown_s,
            value.max_gags_per_viewer,
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
        memory_service: Any = None,
    ) -> None:
        self._store = store
        self.limits = limits
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._enabled = bool(enabled)
        self._evidence_exists = evidence_exists or (lambda _event_id: False)
        self._memory_service = memory_service
        self._running = False
        self._accepted = 0
        self._duplicates = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, store: RelationshipStore | None = None, metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
        evidence_exists: Callable[[str], bool] | None = None,
        memory_service: Any = None,
    ) -> "RelationshipManager":
        return cls(
            store or RelationshipStore(loader.get("system", "paths.db_file", "data/mai.db")),
            RelationshipLimits.from_loader(loader), metrics=metrics, clock=clock,
            enabled=enabled,
            evidence_exists=evidence_exists,
            memory_service=memory_service,
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
        emotion_category: str | None = None,
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
            if emotion_category in self.limits.positive_emotion_categories:
                self._store.record_positive_event(
                    event_id=event_id, viewer_id=viewer_id,
                    evidence_id=f"agent:chat:{event_id}", occurred_at=occurred_at,
                )
                self._record("accepted", "positive_interaction")
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
        narratives = tuple(item for item in self._store.list_narratives() if item.expires_at > now)
        gags = self._store.list_running_gags()
        return RelationshipSnapshot(
            profiles=profiles, notes=notes, narratives=narratives, running_gags=gags,
        )

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

    def create_narrative(
        self, *, summary: str, event_refs: list[str], reason: str,
        viewer_id: str | None = None,
    ) -> NarrativeItem | None:
        clean = self._clean(summary)
        if (
            not self._enabled or not clean or not reason.strip()
            or not self._valid_evidence(event_refs)
            or (viewer_id is not None and self.get_profile(viewer_id) is None)
        ):
            self._record("rejected", "narrative_validation")
            return None
        active = [
            item for item in self._store.list_narratives()
            if item.status is NarrativeStatus.ACTIVE and item.expires_at > _utc(self._clock())
        ]
        if len(active) >= self.limits.max_narratives:
            self._record("rejected", "narrative_cap")
            return None
        now = _utc(self._clock())
        item = NarrativeItem(
            narrative_id=f"narrative:{uuid.uuid4().hex}", viewer_id=viewer_id,
            summary=clean, event_refs=tuple(dict.fromkeys(event_refs)),
            status=NarrativeStatus.ACTIVE, created_at=now,
            expires_at=now + timedelta(days=self.limits.narrative_ttl_days),
        )
        self._store.insert_narrative(item)
        self._audit("narrative_create", viewer_id, item.narrative_id, reason)
        self._record("created", "narrative")
        return item

    def resolve_narrative(self, narrative_id: str, *, reason: str) -> bool:
        item = self._store.get_narrative(narrative_id)
        if item is None or not reason.strip():
            return False
        ok = self._store.resolve_narrative(narrative_id)
        if ok:
            self._audit("narrative_resolve", item.viewer_id, narrative_id, reason)
            self._record("resolved", "narrative")
        return ok

    def create_running_gag(
        self, viewer_id: str, *, summary: str, event_refs: list[str], reason: str,
    ) -> RunningGag | None:
        clean = self._clean(summary)
        positive_refs = self._store.positive_evidence(viewer_id)
        refs = tuple(dict.fromkeys(str(item).strip() for item in event_refs if str(item).strip()))
        if (
            not self._enabled or self.get_profile(viewer_id) is None or not clean
            or not reason.strip() or len(positive_refs) < self.limits.positive_interactions_required
            or len(refs) < self.limits.positive_interactions_required
            or not set(refs).issubset(set(positive_refs))
        ):
            self._record("rejected", "running_gag_validation")
            return None
        if len(self._store.list_running_gags(viewer_id)) >= self.limits.max_gags_per_viewer:
            self._record("rejected", "running_gag_cap")
            return None
        now = _utc(self._clock())
        gag = RunningGag(
            gag_id=f"gag:{uuid.uuid4().hex}", viewer_id=viewer_id, summary=clean,
            event_refs=refs, status=ReviewStatus.PENDING,
            positive_count=len(positive_refs), created_at=now,
        )
        self._store.insert_running_gag(gag)
        self._audit("running_gag_create", viewer_id, gag.gag_id, reason)
        self._record("created", "running_gag_pending")
        return gag

    def review_running_gag(self, gag_id: str, *, approve: bool, reason: str) -> bool:
        gag = self._store.get_running_gag(gag_id)
        if gag is None or not reason.strip():
            return False
        status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        ok = self._store.review_running_gag(gag_id, status)
        if ok:
            self._audit("running_gag_review", gag.viewer_id, gag_id, reason)
            self._record("reviewed", f"running_gag_{status.value}")
        return ok

    def set_memory_service(self, memory_service: Any = None) -> None:
        self._memory_service = memory_service

    async def export_viewer(self, viewer_id: str) -> dict:
        if not viewer_id.startswith("v_"):
            raise ValueError("privacy export requires pseudonymous viewer id")
        data = self._store.export_viewer(viewer_id)
        memory_records: list[dict[str, Any]] = []
        if self._memory_service is not None:
            entries = await self._memory_service.export_viewer(viewer_id)
            for entry in entries:
                memory_records.append({
                    "entry_id": entry.entry_id,
                    "content": mask_pii(entry.content) or "",
                    "timestamp": entry.timestamp.isoformat(),
                    "tier": entry.tier.value,
                    "tags": list(entry.tags),
                    "importance": entry.importance,
                })
        data["memory"] = memory_records
        self._record("exported", "viewer_privacy")
        return data

    async def delete_viewer(self, viewer_id: str, *, reason: str) -> dict | None:
        if not viewer_id.startswith("v_") or not reason.strip():
            return None
        memory_deleted = 0
        if self._memory_service is not None:
            # Strict order: if memory deletion fails, retain relationship data and surface failure.
            memory_deleted = await self._memory_service.forget_viewer(viewer_id)
        counts = self._store.delete_viewer(
            viewer_id, reason=self._clean(reason), created_at=_utc(self._clock()),
        )
        self._record("deleted", "viewer_privacy")
        return {"viewer_id": viewer_id, "memory": memory_deleted, "relationships": counts}

    def render_context(self, raw_viewer_id: str | None = None) -> str:
        if not self._enabled:
            return ""
        now = _utc(self._clock())
        viewer_id = hash_viewer_id(raw_viewer_id)
        lines = ["[Grounded relationship/narrative — do not infer beyond evidence]"]
        if viewer_id is not None:
            profile = self.get_profile(viewer_id)
            if profile is not None:
                attrs: list[str] = []
                if profile.confirmed_preferences:
                    attrs.append("preferences=" + ", ".join(profile.confirmed_preferences))
                if profile.boundaries:
                    attrs.append("boundaries=" + ", ".join(profile.boundaries))
                if profile.tone:
                    attrs.append("tone=" + profile.tone)
                if attrs:
                    lines.append("Current viewer confirmed: " + "; ".join(attrs))
                approved = [
                    note for note in self._store.list_notes(viewer_id)
                    if note.status is ReviewStatus.APPROVED and note.expires_at > now
                ]
                for note in approved[: self.limits.prompt_max_items]:
                    lines.append(
                        f"Approved note [evidence={','.join(note.evidence_refs)}]: {note.summary}"
                    )
                ready_gags = [
                    gag for gag in self._store.list_running_gags(viewer_id)
                    if gag.status is ReviewStatus.APPROVED and (
                        gag.last_referenced_at is None
                        or (now - gag.last_referenced_at).total_seconds()
                        >= self.limits.gag_reference_cooldown_s
                    )
                ]
                if ready_gags:
                    gag = ready_gags[0]
                    lines.append(
                        f"Approved running gag [gag_id={gag.gag_id}; "
                        f"evidence={','.join(gag.event_refs)}]: {gag.summary}"
                    )
                    self._store.mark_running_gag_referenced(gag.gag_id, now)
                    self._record("referenced", "running_gag")
        narratives = [
            item for item in self._store.list_narratives()
            if item.status is NarrativeStatus.ACTIVE and item.expires_at > now
            and (item.viewer_id is None or item.viewer_id == viewer_id)
        ]
        for item in narratives[: self.limits.prompt_max_items]:
            lines.append(
                f"Active narrative [evidence={','.join(item.event_refs)}]: {item.summary}"
            )
        if len(lines) == 1:
            return ""
        return "\n".join(lines)[: self.limits.prompt_max_chars]

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
