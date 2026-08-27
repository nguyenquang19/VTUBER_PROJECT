"""Deterministic pseudonymous viewer profile manager (M7.1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, MemoryTier, RecallDecision, RecallGateService
from interfaces.relationship import RelationshipService
from services.data.sanitize import hash_viewer_id
from services.data.sanitize import mask_pii
from services.relationship.store import RelationshipStore
from interfaces.relationship import (
    RelationshipNote,
    RelationshipContextHint,
    RelationshipHintKind,
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
    regular_min_interactions: int = 3
    regular_last_seen_days: int = 14
    context_fact_slots_max: int = 2
    callback_frequency_window_s: int = 3600
    callback_frequency_cap: int = 1

    @classmethod
    def from_loader(cls, loader: Any) -> "RelationshipLimits":
        prefix = "relationships."
        value = cls(
            profile_ttl_days=int(loader.get("state", prefix + "profile_ttl_days", 30)),
            seen_event_ttl_days=int(loader.get("state", prefix + "seen_event_ttl_days", 30)),
            max_profiles_snapshot=int(loader.get("state", prefix + "max_profiles_snapshot", 100)),
            max_preferences=int(loader.get("state", prefix + "max_preferences", 5)),
            max_boundaries=int(loader.get("state", prefix + "max_boundaries", 5)),
            max_text_chars=int(loader.get("state", prefix + "max_text_chars", 240)),
            note_ttl_days=int(loader.get("state", "notes.ttl_days", 30)),
            max_notes_per_viewer=int(loader.get("state", "notes.max_per_viewer", 20)),
            narrative_ttl_days=int(loader.get("state", "narrative.ttl_days", 30)),
            max_narratives=int(loader.get("state", "narrative.max_items", 50)),
            prompt_max_items=int(loader.get("state", "narrative.prompt_max_items", 2)),
            prompt_max_chars=int(loader.get("state", "narrative.prompt_max_chars", 360)),
            positive_interactions_required=int(loader.get(
                "state", "running_gags.positive_interactions_required", 3,
            )),
            positive_emotion_categories=tuple(str(item) for item in loader.get(
                "state", "running_gags.positive_emotion_categories",
                ["chat_compliment", "chat_mention_direct", "donation_small", "donation_large"],
            )),
            gag_reference_cooldown_s=int(loader.get(
                "state", "running_gags.reference_cooldown_seconds", 1800,
            )),
            max_gags_per_viewer=int(loader.get(
                "state", "running_gags.max_per_viewer", 5,
            )),
            regular_min_interactions=int(loader.get(
                "state", prefix + "regular_min_interactions", 3,
            )),
            regular_last_seen_days=int(loader.get(
                "state", prefix + "regular_last_seen_days", 14,
            )),
            context_fact_slots_max=int(loader.get(
                "state", prefix + "context_fact_slots_max", 2,
            )),
            callback_frequency_window_s=int(loader.get(
                "state", "running_gags.callback_frequency_window_seconds", 3600,
            )),
            callback_frequency_cap=int(loader.get(
                "state", "running_gags.callback_frequency_cap", 1,
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
            value.regular_min_interactions, value.regular_last_seen_days,
            value.context_fact_slots_max, value.callback_frequency_window_s,
            value.callback_frequency_cap,
        ) <= 0:
            raise ValueError("relationship limits must be positive")
        if value.context_fact_slots_max > value.max_notes_per_viewer:
            raise ValueError("relationship fact slots must not exceed note storage cap")
        if value.callback_frequency_cap > value.max_gags_per_viewer:
            raise ValueError("relationship callback cap must not exceed gag storage cap")
        return value


@dataclass(frozen=True)
class _ContextCandidate:
    entry: MemoryEntry
    kind: RelationshipHintKind
    evidence_refs: tuple[str, ...]
    observed_at: datetime
    expires_at: datetime
    callback_id: str | None = None


class RelationshipManager(RelationshipService):
    service_id = "relationship_manager"

    def __init__(
        self, store: RelationshipStore, limits: RelationshipLimits, *,
        metrics: Any = None, clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
        context_enabled: bool = False,
        evidence_exists: Callable[[str], bool] | None = None,
        memory_service: Any = None,
        recall_gate: RecallGateService | None = None,
    ) -> None:
        self._store = store
        self.limits = limits
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._enabled = bool(enabled)
        if not isinstance(context_enabled, bool):
            raise ValueError("context_enabled must be boolean")
        if recall_gate is not None and not isinstance(recall_gate, RecallGateService):
            raise ValueError("recall_gate must implement RecallGateService")
        self._context_enabled = context_enabled
        self._evidence_exists = evidence_exists or (lambda _event_id: False)
        self._memory_service = memory_service
        self._recall_gate = recall_gate
        self._running = False
        self._accepted = 0
        self._duplicates = 0
        self._context_counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, store: RelationshipStore | None = None, metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool = True,
        context_enabled: bool = False,
        evidence_exists: Callable[[str], bool] | None = None,
        memory_service: Any = None,
        recall_gate: RecallGateService | None = None,
    ) -> "RelationshipManager":
        return cls(
            store or RelationshipStore(loader.get("system", "paths.db_file", "data/mai.db")),
            RelationshipLimits.from_loader(loader), metrics=metrics, clock=clock,
            enabled=enabled,
            context_enabled=context_enabled,
            evidence_exists=evidence_exists,
            memory_service=memory_service,
            recall_gate=recall_gate,
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
        values = {
            "relationship_interactions_total": self._accepted,
            "relationship_duplicates_total": self._duplicates,
            "relationship_profiles": len(self.snapshot().profiles),
            "relationship_enabled": self._enabled,
            "relationship_context_enabled": self._context_enabled,
        }
        for name, count in sorted(self._context_counts.items()):
            values[f"relationship_context_{name}_total"] = count
        return values

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

    def context_hints(
        self, *, raw_viewer_id: str | None = None, event_ref: str | None = None,
    ) -> tuple[RelationshipContextHint, ...]:
        if not self._enabled or not self._context_enabled:
            return ()
        if raw_viewer_id is not None and event_ref is not None:
            raise ValueError("relationship context accepts one identity source")
        viewer_id = self._resolve_context_viewer(raw_viewer_id, event_ref)
        if viewer_id is None:
            self._context_record("stranger")
            return ()
        now = _utc(self._clock())
        profile = self._store.get_profile(viewer_id)
        if profile is None:
            self._context_record("stranger")
            return ()
        if (
            profile.expires_at <= now
            or now - profile.last_seen > timedelta(days=self.limits.regular_last_seen_days)
        ):
            self._context_record("stale")
            return ()
        if profile.interaction_count < self.limits.regular_min_interactions:
            self._context_record("stranger")
            return ()
        self._context_record("regular")
        hints = [RelationshipContextHint(
            hint_id=f"relationship-tone:{viewer_id}",
            viewer_ref=viewer_id,
            kind=RelationshipHintKind.TONE,
            instruction=(
                "Treat the current viewer as a returning regular: use a slightly warmer, "
                "more familiar tone without claiming or listing personal details."
            ),
            evidence_refs=(f"relationship:profile:{viewer_id}",),
            observed_at=profile.last_seen,
            expires_at=profile.expires_at,
            salience=1.0,
        )]
        self._context_record("tone_hint")
        candidates = self._context_candidates(profile, now)
        if not candidates:
            return tuple(hints)
        for candidate in candidates:
            self._context_record(f"{candidate.kind.value}_considered")
        gate = self._recall_gate
        if gate is None or not gate.enabled:
            self._context_record("gate_unavailable")
            for candidate in candidates:
                self._context_record(f"{candidate.kind.value}_suppressed")
            return tuple(hints)
        try:
            decisions = gate.evaluate(
                tuple(candidate.entry for candidate in candidates), now=now,
            )
            if len(decisions) != len(candidates):
                raise ValueError("relationship recall decision count mismatch")
            projected: list[tuple[_ContextCandidate, RelationshipContextHint]] = []
            for candidate, decision in zip(candidates, decisions, strict=True):
                if (
                    not isinstance(decision, RecallDecision)
                    or decision.memory_ref != candidate.entry.entry_id
                ):
                    raise ValueError("relationship recall provenance mismatch")
                if not decision.surface:
                    continue
                if decision.latent_hint is None:
                    raise ValueError("surfaced relationship recall requires a hint")
                projected.append((candidate, RelationshipContextHint(
                    hint_id=f"relationship-hint:{candidate.entry.entry_id}",
                    viewer_ref=viewer_id,
                    kind=candidate.kind,
                    instruction=decision.latent_hint,
                    evidence_refs=candidate.evidence_refs,
                    observed_at=candidate.observed_at,
                    expires_at=candidate.expires_at,
                    salience=decision.salience,
                )))
            for candidate, decision in zip(candidates, decisions, strict=True):
                if not decision.surface:
                    self._context_record(f"{candidate.kind.value}_suppressed")
            for candidate, hint in projected:
                hints.append(hint)
                self._context_record(f"{candidate.kind.value}_surfaced")
                if candidate.callback_id is not None:
                    self._store.mark_running_gag_referenced(candidate.callback_id, now)
        except Exception:
            self._context_record("gate_error")
            for candidate in candidates:
                self._context_record(f"{candidate.kind.value}_suppressed")
            return tuple(hints)
        return tuple(hints)

    def render_context(self, raw_viewer_id: str | None = None) -> str:
        if not self._enabled:
            return ""
        if not self._context_enabled:
            return self._render_m7_context(raw_viewer_id)
        hints = self.context_hints(raw_viewer_id=raw_viewer_id)
        if not hints:
            return ""
        lines = ["[Latent relationship context — never recite a viewer profile]"]
        for hint in hints:
            lines.append(f"{hint.kind.value.title()} hint: {hint.instruction}")
        return "\n".join(lines)[: self.limits.prompt_max_chars]

    def _render_m7_context(self, raw_viewer_id: str | None) -> str:
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

    def _resolve_context_viewer(
        self, raw_viewer_id: str | None, event_ref: str | None,
    ) -> str | None:
        if raw_viewer_id is not None:
            return hash_viewer_id(raw_viewer_id)
        if event_ref is None or not isinstance(event_ref, str) or not event_ref.strip():
            return None
        event_id = event_ref.strip().removeprefix("agent:chat:")
        viewer_id = self._store.viewer_for_event(event_id)
        if viewer_id is None:
            self._context_record("lineage_miss")
        return viewer_id

    def _context_candidates(
        self, profile: ViewerProfile, now: datetime,
    ) -> tuple[_ContextCandidate, ...]:
        candidates: list[_ContextCandidate] = []
        gags = self._store.list_running_gags(profile.viewer_id)
        recent_callbacks = sum(
            1 for gag in gags
            if gag.last_referenced_at is not None
            and 0.0 <= (now - gag.last_referenced_at).total_seconds()
            < self.limits.callback_frequency_window_s
        )
        approved = [gag for gag in gags if gag.status is ReviewStatus.APPROVED]
        if recent_callbacks < self.limits.callback_frequency_cap:
            ready = [gag for gag in approved if (
                gag.last_referenced_at is None
                or (now - gag.last_referenced_at).total_seconds()
                >= self.limits.gag_reference_cooldown_s
            )]
            if ready:
                gag = ready[0]
                candidates.append(_ContextCandidate(
                    entry=self._latent_candidate_entry(
                        entry_id=gag.gag_id,
                        observed_at=gag.created_at,
                        importance=0.9,
                        cognitive_kind="RELATIONSHIP_CALLBACK",
                    ),
                    kind=RelationshipHintKind.CALLBACK,
                    evidence_refs=gag.event_refs,
                    observed_at=gag.created_at,
                    expires_at=profile.expires_at,
                    callback_id=gag.gag_id,
                ))
            elif approved:
                self._context_record("callback_considered")
                self._context_record("callback_suppressed")
        elif approved:
            self._context_record("callback_considered")
            self._context_record("callback_suppressed")
        facts: list[_ContextCandidate] = []
        if profile.confirmed_preferences or profile.boundaries or profile.tone:
            facts.append(_ContextCandidate(
                entry=self._latent_candidate_entry(
                    entry_id=f"relationship-profile-fact:{profile.viewer_id}",
                    observed_at=profile.last_seen,
                    importance=0.75,
                    cognitive_kind="PREFERENCE",
                ),
                kind=RelationshipHintKind.FACT,
                evidence_refs=(f"relationship:profile:{profile.viewer_id}",),
                observed_at=profile.last_seen,
                expires_at=profile.expires_at,
            ))
        approved_notes = [
            note for note in self._store.list_notes(profile.viewer_id)
            if note.status is ReviewStatus.APPROVED and note.expires_at > now
        ]
        for note in approved_notes:
            facts.append(_ContextCandidate(
                entry=self._latent_candidate_entry(
                    entry_id=note.note_id,
                    observed_at=note.created_at,
                    importance=0.7,
                    cognitive_kind="RELATIONSHIP_NOTE",
                ),
                kind=RelationshipHintKind.FACT,
                evidence_refs=note.evidence_refs,
                observed_at=note.created_at,
                expires_at=note.expires_at,
            ))
        candidates.extend(facts[: self.limits.context_fact_slots_max])
        return tuple(candidates)

    def _latent_candidate_entry(
        self, *, entry_id: str, observed_at: datetime, importance: float,
        cognitive_kind: str,
    ) -> MemoryEntry:
        return MemoryEntry(
            entry_id=entry_id,
            content="relationship latent candidate",
            timestamp=observed_at,
            importance=importance,
            tier=MemoryTier.PERSISTENT,
            metadata={"cognitive_kind": cognitive_kind},
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def context_enabled(self) -> bool:
        return self._context_enabled

    def set_context_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("relationship context enabled state must be boolean")
        self._context_enabled = enabled

    def set_recall_gate(self, recall_gate: RecallGateService | None) -> None:
        if recall_gate is not None and not isinstance(recall_gate, RecallGateService):
            raise ValueError("recall_gate must implement RecallGateService")
        self._recall_gate = recall_gate

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

    def _context_record(self, outcome: str) -> None:
        self._context_counts[outcome] = self._context_counts.get(outcome, 0) + 1


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
