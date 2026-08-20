"""Deterministic single-active GoalManager state machine (Master Plan M2.1)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from interfaces.agent import GoalManagerService
from interfaces.base import HealthStatus
from services.agent.goal_types import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
    ShortIntention,
    ShortIntentionStatus,
)
from services.agent.types import (
    AgentEventKind, AgentEventSource, AgentStateSnapshot, EventProvenance, GroundedEvent,
    ThreadStatus,
)


_THREAD_BOUND_GOAL_KINDS = frozenset({
    GoalKind.CONTINUE_THREAD,
    GoalKind.ANSWER_FOLLOW_UP,
})


@dataclass(frozen=True)
class GoalLimits:
    candidates_max: int
    suspended_max: int
    terminal_history_max: int
    metadata_text_max_chars: int
    operator_priority: int = 90
    operator_ttl_s: int = 3600
    short_intention_max_steps: int = 3
    proposal_allowed_kinds: tuple[GoalKind, ...] = (
        GoalKind.CONTINUE_THREAD, GoalKind.WAIT_FOR_CHAT_ANSWER,
    )
    action_failure_policy: str = "fail"
    action_cancellation_policy: str = "cancel"

    def __post_init__(self) -> None:
        for name in (
            "candidates_max", "suspended_max", "terminal_history_max",
            "metadata_text_max_chars", "operator_ttl_s", "short_intention_max_steps",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"goal_manager.{name} must be a positive integer")
        if (
            isinstance(self.operator_priority, bool)
            or not isinstance(self.operator_priority, int)
            or self.operator_priority < 0
        ):
            raise ValueError("priorities.operator_pinned must be a non-negative integer")
        if not 1 <= self.short_intention_max_steps <= 3:
            raise ValueError("goal_manager.short_intention_max_steps must be between one and three")
        if (
            not isinstance(self.proposal_allowed_kinds, tuple)
            or not self.proposal_allowed_kinds
            or not all(isinstance(item, GoalKind) for item in self.proposal_allowed_kinds)
            or len(set(self.proposal_allowed_kinds)) != len(self.proposal_allowed_kinds)
        ):
            raise ValueError("proposal.allowed_kinds must contain unique GoalKind values")
        if self.action_failure_policy != "fail":
            raise ValueError("goal_manager.action_failure_policy must be 'fail'")
        if self.action_cancellation_policy != "cancel":
            raise ValueError("goal_manager.action_cancellation_policy must be 'cancel'")

    @classmethod
    def from_loader(cls, loader: Any) -> "GoalLimits":
        prefix = "goal_manager."
        raw_kinds = loader.get("agent_goals", "proposal.allowed_kinds", None)
        if not isinstance(raw_kinds, list) or not raw_kinds or not all(
            isinstance(value, str) and value.strip() for value in raw_kinds
        ):
            raise ValueError("proposal.allowed_kinds must be a non-empty list of strings")
        try:
            allowed_kinds = tuple(GoalKind(value.strip()) for value in raw_kinds)
        except ValueError as exc:
            raise ValueError("proposal.allowed_kinds contains an unsupported goal kind") from exc
        return cls(
            candidates_max=loader.get("agent_goals", prefix + "candidates_max", None),
            suspended_max=loader.get("agent_goals", prefix + "suspended_max", None),
            terminal_history_max=loader.get(
                "agent_goals", prefix + "terminal_history_max", None,
            ),
            metadata_text_max_chars=loader.get(
                "agent_goals", prefix + "metadata_text_max_chars", None,
            ),
            short_intention_max_steps=loader.get(
                "agent_goals", prefix + "short_intention_max_steps", None,
            ),
            operator_priority=loader.get(
                "agent_goals", "priorities.operator_pinned", None,
            ),
            operator_ttl_s=loader.get(
                "agent_goals", prefix + "operator_pinned_ttl_s", None,
            ),
            proposal_allowed_kinds=allowed_kinds,
            action_failure_policy=loader.get(
                "agent_goals", prefix + "action_failure_policy", None,
            ),
            action_cancellation_policy=loader.get(
                "agent_goals", prefix + "action_cancellation_policy", None,
            ),
        )


class GoalManager(GoalManagerService):
    service_id = "goal_manager"

    def __init__(
        self,
        limits: GoalLimits,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
        on_active_changed: Callable[[str | None], None] | None = None,
        audit_sink: Callable[[GroundedEvent], bool] | None = None,
        agenda_policy: Any = None,
        mood_provider: Callable[[], Any] | None = None,
        tone_flags_provider: Callable[[], set[str]] | None = None,
    ) -> None:
        self.limits = limits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._on_active_changed = on_active_changed
        self._audit_sink = audit_sink
        self._agenda_policy = agenda_policy
        self._mood_provider = mood_provider
        self._tone_flags_provider = tone_flags_provider
        self._snapshot = GoalSnapshot()
        self._intentions: dict[str, ShortIntention] = {}
        self._recent_intentions: tuple[ShortIntention, ...] = ()
        self._running = False
        self._counts: dict[str, int] = {}
        self._intention_counts: dict[str, int] = {}
        self._audit_seq = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
        on_active_changed: Callable[[str | None], None] | None = None,
        audit_sink: Callable[[GroundedEvent], bool] | None = None,
        agenda_policy: Any = None,
        mood_provider: Callable[[], Any] | None = None,
        tone_flags_provider: Callable[[], set[str]] | None = None,
    ) -> "GoalManager":
        return cls(
            GoalLimits.from_loader(loader), clock=clock, metrics=metrics,
            on_active_changed=on_active_changed,
            audit_sink=audit_sink,
            agenda_policy=agenda_policy,
            mood_provider=mood_provider,
            tone_flags_provider=tone_flags_provider,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, active_goal=self._snapshot.active.goal_id if self._snapshot.active else None,
        )

    def get_metrics(self) -> dict[str, Any]:
        active = self._snapshot.active
        age = max(0.0, (_utc(self._clock()) - active.created_at).total_seconds()) if active else 0.0
        intention = self._current_intention()
        intention_age = (
            max(0.0, (_utc(self._clock()) - intention.created_at).total_seconds())
            if intention is not None else 0.0
        )
        if self._metrics is not None and hasattr(self._metrics, "set_goal_active_age"):
            try:
                self._metrics.set_goal_active_age(age)
            except Exception:
                pass
        if self._metrics is not None and hasattr(self._metrics, "set_intention_active"):
            try:
                self._metrics.set_intention_active(
                    intention_age,
                    intention.step_index + 1 if intention is not None else 0,
                )
            except Exception:
                pass
        return {
            **{f"goal_{name}_total": count for name, count in sorted(self._counts.items())},
            **{
                f"intention_{name}_total": count
                for name, count in sorted(self._intention_counts.items())
            },
            "goal_active_age_seconds": age,
            "goal_candidates": len(self._snapshot.candidates),
            "goal_suspended": len(self._snapshot.suspended),
            "intention_active_age_seconds": intention_age,
            "intention_current_step": intention.step_index + 1 if intention else 0,
            "intention_recent_terminal": len(self._recent_intentions),
        }

    def submit(self, goal: Goal) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        if (goal.status is not GoalStatus.CANDIDATE or goal.expires_at <= now
                or len(goal.steps) > self.limits.short_intention_max_steps):
            self._record("rejected", goal.kind.value)
            return False
        if self._find(goal.goal_id) is not None:
            self._record("rejected", "duplicate")
            return False
        goal = self._bounded_goal(goal)
        self._record("created", goal.kind.value)
        active = self._snapshot.active
        if active is None:
            self._propose_intention(goal)
            self._activate(goal)
            return True
        if goal.priority > active.priority:
            suspended = active.with_status(
                GoalStatus.SUSPENDED, suspend_reason=f"preempted_by:{goal.goal_id}",
            )
            self._transition_intention(
                active.goal_id, ShortIntentionStatus.SUSPENDED,
                reason_code=f"preempted_by:{goal.goal_id}",
            )
            self._record_intention("suspended", active.kind.value)
            combined = (*self._snapshot.suspended, suspended)
            bounded = self._rank(combined)[: self.limits.suspended_max]
            self._snapshot = replace(
                self._snapshot,
                active=None,
                suspended=bounded,
            )
            self._terminalize_dropped(
                tuple(item for item in combined if item not in bounded),
                reason="suspended_capacity",
            )
            self._record("preempted", active.kind.value)
            self._propose_intention(goal)
            self._activate(goal)
            return True
        combined = (*self._snapshot.candidates, goal)
        candidates = self._rank(combined)[: self.limits.candidates_max]
        accepted = any(item.goal_id == goal.goal_id for item in candidates)
        self._snapshot = replace(self._snapshot, candidates=candidates)
        if accepted:
            self._propose_intention(goal)
            self._terminalize_dropped(
                tuple(item for item in combined if item not in candidates),
                reason="candidate_capacity",
            )
        else:
            self._record("rejected", "candidate_cap")
            self._record_intention("rejected", "candidate_capacity")
        return accepted

    def complete(self, goal_id: str, *, reason: str = "success") -> bool:
        return self._complete_or_advance(goal_id, reason=reason, outcome_id=None)

    def cancel(self, goal_id: str, *, reason: str) -> bool:
        return self._terminal(goal_id, GoalStatus.CANCELLED, reason)

    def fail(self, goal_id: str, *, reason: str) -> bool:
        """Terminalize a failed short intention; never create a retry or successor."""
        return self._terminal(goal_id, GoalStatus.FAILED, reason)

    def record_action_outcome(
        self,
        goal_id: str,
        intention_id: str,
        outcome_id: str,
        *,
        outcome: str,
        reason: str,
    ) -> bool:
        """Apply one authoritative action outcome to the expected active intention."""
        if not all(isinstance(value, str) and value.strip() for value in (
            goal_id, intention_id, outcome_id, outcome, reason,
        )):
            self._record_intention("rejected", "invalid_action_outcome")
            return False
        active = self._snapshot.active
        intention = self._current_intention()
        if (
            active is None
            or active.goal_id != goal_id.strip()
            or intention is None
            or intention.intention_id != intention_id.strip()
        ):
            self._record_intention("rejected", "stale_action_outcome")
            return False
        clean_outcome_id = outcome_id.strip()
        if clean_outcome_id in intention.applied_outcome_ids:
            self._record_intention("rejected", "duplicate_action_outcome")
            return False
        outcome_value = outcome.strip()
        if outcome_value == "succeeded":
            return self._complete_or_advance(
                active.goal_id, reason=reason, outcome_id=clean_outcome_id,
            )
        if outcome_value == "failed":
            if self.limits.action_failure_policy != "fail":
                return False
            return self._terminal(
                active.goal_id, GoalStatus.FAILED, reason,
                outcome_id=clean_outcome_id,
            )
        if outcome_value == "cancelled":
            if self.limits.action_cancellation_policy != "cancel":
                return False
            return self._terminal(
                active.goal_id, GoalStatus.CANCELLED, reason,
                outcome_id=clean_outcome_id,
            )
        self._record_intention("rejected", "unsupported_action_outcome")
        return False

    def snapshot(self) -> GoalSnapshot:
        self._prune(_utc(self._clock()))
        live_goals = self._all_goals()
        return GoalSnapshot(
            active=self._snapshot.active,
            candidates=self._snapshot.candidates,
            suspended=self._snapshot.suspended,
            recent_terminal=self._snapshot.recent_terminal,
            current_intention=self._current_intention(),
            intentions=tuple(
                self._intentions[goal.goal_id]
                for goal in live_goals
                if goal.goal_id in self._intentions
            ),
            recent_intentions=self._recent_intentions,
        )

    def reconcile_threads(self, open_thread_ids: set[str] | tuple[str, ...]) -> int:
        """Remove invalid thread goals before they can block Director arbitration."""
        now = _utc(self._clock())
        self._prune(now)
        available = {str(thread_id) for thread_id in open_thread_ids if str(thread_id)}
        goals = (
            *((self._snapshot.active,) if self._snapshot.active else ()),
            *self._snapshot.candidates,
            *self._snapshot.suspended,
        )
        stale = tuple(
            goal for goal in goals
            if (
                goal.kind in _THREAD_BOUND_GOAL_KINDS
                and bool(goal.parent_thread_id)
                and goal.parent_thread_id not in available
            )
        )
        if not stale:
            return 0
        for goal in stale:
            current = self._find(goal.goal_id)
            if current is None:
                continue
            self._remove(current.goal_id)
            self._terminal_intention_for_goal(
                current.goal_id,
                ShortIntentionStatus.CANCELLED,
                reason_code="parent_thread_missing",
            )
            self._intentions.pop(current.goal_id, None)
            self._append_terminal(current.with_status(
                GoalStatus.CANCELLED,
                suspend_reason="parent_thread_missing",
            ))
            self._record("cancelled", current.kind.value)
            self._record("reconciled", "parent_thread_missing")
        self._activate_next(now)
        return len(stale)

    def focus_delivered_thread(
        self,
        parent_thread_id: str | None,
        *,
        source_event_ids: set[str] | tuple[str, ...] = (),
        reason: str = "targeted_chat_delivered",
    ) -> int:
        """Focus the thread whose source chat reached delivery.

        Soft continuations for other parents are cancelled so a later goal cannot
        jump back across the public topic boundary. At most one continuation for
        the focused parent is retained and marked delivery-eligible.
        """
        now = _utc(self._clock())
        self._prune(now)
        source_ids = {str(value) for value in source_event_ids if str(value)}
        continuations = tuple(
            goal for goal in self._all_goals()
            if goal.kind is GoalKind.CONTINUE_THREAD
        )
        matching = tuple(
            goal for goal in continuations
            if parent_thread_id and goal.parent_thread_id == parent_thread_id
        )
        source_matching = tuple(
            goal for goal in matching
            if str(goal.metadata.get("source_event_id") or "") in source_ids
        )
        preferred = self._rank(source_matching or matching)[0] if matching else None
        changed = 0
        for goal in continuations:
            if preferred is not None and goal.goal_id == preferred.goal_id:
                continue
            self._remove(goal.goal_id)
            self._terminal_intention_for_goal(
                goal.goal_id,
                ShortIntentionStatus.CANCELLED,
                reason_code=reason,
            )
            self._intentions.pop(goal.goal_id, None)
            self._append_terminal(goal.with_status(
                GoalStatus.CANCELLED, suspend_reason=reason,
            ))
            self._record("cancelled", goal.kind.value)
            changed += 1

        if preferred is not None:
            current = self._find(preferred.goal_id)
            if current is not None:
                metadata = dict(current.metadata)
                metadata["source_delivered"] = True
                metadata["focused_by"] = reason
                updated = replace(
                    current,
                    metadata=_bound(metadata, self.limits.metadata_text_max_chars),
                )
                self._replace_goal(updated)
                if self._snapshot.active is None:
                    self._remove(updated.goal_id)
                    self._activate(updated.with_status(GoalStatus.CANDIDATE))
                self._record("focused", GoalKind.CONTINUE_THREAD.value)
                changed += 1
        elif self._snapshot.active is None:
            self._activate_next(now)
        return changed

    def clear_continue_threads(self, *, reason: str) -> int:
        """Cancel every pending soft continuation after a delivered room boundary."""
        return self.focus_delivered_thread(None, reason=reason)

    def pin_operator(
        self, *, reason: str, success_condition: str, parent_thread_id: str | None = None,
    ) -> Goal | None:
        reason = _compact(reason, self.limits.metadata_text_max_chars)
        success = _compact(success_condition, self.limits.metadata_text_max_chars)
        if not reason or not success:
            self._record("rejected", "invalid_operator_pin")
            return None
        now = _utc(self._clock())
        goal = Goal(
            goal_id=f"goal:operator:{uuid.uuid4().hex}",
            kind=GoalKind.OPERATOR_PINNED,
            status=GoalStatus.CANDIDATE,
            priority=self.limits.operator_priority,
            reason=reason,
            source=GoalSource.OPERATOR,
            created_at=now,
            expires_at=now + timedelta(seconds=self.limits.operator_ttl_s),
            success_conditions=(success,),
            parent_thread_id=parent_thread_id or None,
            metadata={"relevant": True},
        )
        if not self.submit(goal):
            return None
        self._operator_audit("pin", goal.goal_id, reason)
        return self._find(goal.goal_id)

    def operator_complete(self, goal_id: str, *, reason: str) -> bool:
        clean_reason = _compact(reason, self.limits.metadata_text_max_chars)
        if not self._terminal(goal_id, GoalStatus.COMPLETED, clean_reason):
            return False
        self._operator_audit("complete", goal_id, reason)
        return True

    def operator_cancel(self, goal_id: str, *, reason: str) -> bool:
        if not self.cancel(goal_id, reason=_compact(reason, self.limits.metadata_text_max_chars)):
            return False
        self._operator_audit("cancel", goal_id, reason)
        return True

    def handle_event(self, event: GroundedEvent, state: AgentStateSnapshot) -> None:
        """Consume only accepted grounded events; never calls an LLM."""
        self._prune(_utc(self._clock()))
        before = self._snapshot
        candidates = (
            self._agenda_policy.candidates_for(
                event, state, before,
                mood=self._safe_mood(), tone_flags=self._safe_tone_flags(),
            )
            if self._agenda_policy is not None else ()
        )

        active = before.active
        if event.kind is AgentEventKind.CHAT_RECEIVED and active is not None:
            if active.kind is GoalKind.WAIT_FOR_CHAT_ANSWER:
                self.complete(active.goal_id, reason=f"chat_answer:{event.event_id}")
            elif active.kind is GoalKind.CONTINUE_THREAD:
                self.refresh(
                    active.goal_id,
                    self._agenda_policy.config.ttl_seconds[GoalKind.CONTINUE_THREAD]
                    if self._agenda_policy is not None else 300,
                )

        if event.kind is AgentEventKind.SPEECH_COMPLETED:
            self._complete_from_speech(event, state)

        for candidate in candidates:
            self.submit(candidate)

    def set_mood_context_providers(
        self,
        mood_provider: Callable[[], Any] | None,
        tone_flags_provider: Callable[[], set[str]] | None,
    ) -> None:
        self._mood_provider = mood_provider
        self._tone_flags_provider = tone_flags_provider

    def _safe_mood(self) -> Any:
        try:
            return self._mood_provider() if self._mood_provider is not None else None
        except Exception:
            return None

    def _safe_tone_flags(self) -> set[str]:
        try:
            return set(self._tone_flags_provider() or ()) if self._tone_flags_provider else set()
        except Exception:
            return set()

    def accept_proposal(self, proposal: Any, state: AgentStateSnapshot) -> bool:
        kind = getattr(proposal, "kind", None)
        if kind not in self.limits.proposal_allowed_kinds or self._agenda_policy is None:
            self._record("rejected", "proposal_kind")
            return False
        source_event_id = str(getattr(proposal, "source_event_id", "") or "")
        evidence = next(
            (event for event in state.recent_events if event.event_id == source_event_id), None,
        )
        if evidence is None:
            self._record("rejected", "proposal_evidence")
            return False
        parent = getattr(proposal, "parent_thread_id", None)
        if parent is not None and not any(thread.thread_id == parent for thread in state.open_threads):
            self._record("rejected", "proposal_thread")
            return False
        if kind is GoalKind.CONTINUE_THREAD and parent is None:
            self._record("rejected", "proposal_thread")
            return False
        if kind is GoalKind.WAIT_FOR_CHAT_ANSWER and (
            evidence.kind is not AgentEventKind.SPEECH_FINAL
            or "?" not in str(evidence.payload.get("text") or "")
        ):
            self._record("rejected", "proposal_evidence_kind")
            return False
        now = _utc(self._clock())
        reason = _compact(getattr(proposal, "reason", ""), self.limits.metadata_text_max_chars)
        success = _compact(
            getattr(proposal, "success_condition", ""), self.limits.metadata_text_max_chars,
        )
        if not reason or not success:
            self._record("rejected", "proposal_schema")
            return False
        goal = Goal(
            goal_id=f"goal:proposal:{kind.value}:{source_event_id}",
            kind=kind,
            status=GoalStatus.CANDIDATE,
            priority=self._agenda_policy.config.priorities[kind],
            reason=reason,
            source=GoalSource.LLM_PROPOSAL,
            created_at=now,
            expires_at=now + timedelta(
                seconds=self._agenda_policy.config.ttl_seconds[kind],
            ),
            success_conditions=(success,),
            parent_thread_id=parent,
            metadata={"source_event_id": source_event_id, "relevant": True},
        )
        accepted = self.submit(goal)
        self._record(
            "proposal_accepted" if accepted else "rejected",
            kind.value if accepted else "proposal_submit",
        )
        return accepted

    def refresh(self, goal_id: str, ttl_s: int) -> bool:
        goal = self._find(goal_id)
        if goal is None or ttl_s <= 0:
            return False
        refreshed = replace(goal, expires_at=_utc(self._clock()) + timedelta(seconds=ttl_s))
        if self._snapshot.active and self._snapshot.active.goal_id == goal_id:
            self._snapshot = replace(self._snapshot, active=refreshed)
        else:
            self._snapshot = replace(
                self._snapshot,
                candidates=tuple(refreshed if g.goal_id == goal_id else g for g in self._snapshot.candidates),
                suspended=tuple(refreshed if g.goal_id == goal_id else g for g in self._snapshot.suspended),
            )
        intention = self._intentions.get(goal_id)
        if intention is not None:
            self._intentions[goal_id] = replace(
                intention,
                expires_at=refreshed.expires_at,
                updated_at=_utc(self._clock()),
                reason_code="ttl_refreshed",
            )
        self._record("refreshed", goal.kind.value)
        return True

    def _complete_from_speech(
        self, event: GroundedEvent, state: AgentStateSnapshot,
    ) -> None:
        active = self._snapshot.active
        if active is None:
            return
        event_goal = str(event.payload.get("goal_id") or "")
        if event_goal and event_goal != active.goal_id:
            return
        intention = self._current_intention()
        event_intention = event.payload.get("intention_id")
        if (
            intention is None
            or not isinstance(event_intention, str)
            or event_intention.strip() != intention.intention_id
        ):
            self._record_intention("rejected", "speech_intention_mismatch")
            return
        action = str(event.payload.get("action") or "")
        allowed = {
            GoalKind.ACK_DONATION: {"ack_donation"},
            GoalKind.ANSWER_FOLLOW_UP: {"read_chat", "follow_up"},
            GoalKind.CONTINUE_THREAD: {"continue_thread"},
        }
        if action == "continue_thread" and active.kind is GoalKind.CONTINUE_THREAD:
            self._complete_continue_turn(active, intention, event, state)
            return
        if action in allowed.get(active.kind, set()):
            self.record_action_outcome(
                active.goal_id,
                intention.intention_id,
                event.event_id,
                outcome="succeeded",
                reason=f"speech_completed:{event.event_id}",
            )
            return
        marker = {
            (GoalKind.WAIT_FOR_CHAT_ANSWER, "ask_follow_up"): "follow_up_asked",
            (GoalKind.OPERATOR_PINNED, "share_goal_progress"): "progress_shared",
        }.get((active.kind, action))
        if marker is not None:
            self._mark_active_metadata(active.goal_id, marker, event.event_id)

    def _mark_active_metadata(self, goal_id: str, marker: str, event_id: str) -> None:
        active = self._snapshot.active
        if active is None or active.goal_id != goal_id or active.metadata.get(marker):
            return
        metadata = dict(active.metadata)
        metadata[marker] = True
        metadata[f"{marker}_event_id"] = _compact(
            event_id, self.limits.metadata_text_max_chars,
        )
        self._snapshot = replace(
            self._snapshot,
            active=replace(active, metadata=_bound(metadata, self.limits.metadata_text_max_chars)),
        )
        self._record("progress", marker)

    def _complete_continue_turn(
        self,
        active: Goal,
        intention: ShortIntention,
        event: GroundedEvent,
        state: AgentStateSnapshot,
    ) -> None:
        """Commit one delivered move and atomically retain its parent boundary."""
        now = _utc(self._clock())
        if intention.step_index + 1 < intention.step_count:
            self.record_action_outcome(
                active.goal_id,
                intention.intention_id,
                event.event_id,
                outcome="succeeded",
                reason=f"speech_completed:{event.event_id}",
            )
            return
        parent_id = active.parent_thread_id
        duplicates = tuple(
            goal for goal in self._all_goals()
            if (
                goal.kind is GoalKind.CONTINUE_THREAD
                and goal.parent_thread_id == parent_id
                and goal.goal_id != active.goal_id
            )
        )
        self._complete_intention(
            intention,
            reason_code=f"speech_completed:{event.event_id}",
            outcome_id=event.event_id,
        )
        self._remove(active.goal_id)
        self._intentions.pop(active.goal_id, None)
        self._append_terminal(active.with_status(
            GoalStatus.COMPLETED,
            suspend_reason=f"speech_completed:{event.event_id}",
        ))
        self._record("completed", active.kind.value)
        for goal in duplicates:
            self._remove(goal.goal_id)
            self._terminal_intention_for_goal(
                goal.goal_id,
                ShortIntentionStatus.CANCELLED,
                reason_code="superseded_by_delivered_move",
            )
            self._intentions.pop(goal.goal_id, None)
            self._append_terminal(goal.with_status(
                GoalStatus.CANCELLED,
                suspend_reason="superseded_by_delivered_move",
            ))
            self._record("cancelled", goal.kind.value)

        move = str(event.payload.get("conversation_move") or "").strip().lower()
        thread = next(
            (item for item in state.open_threads if item.thread_id == parent_id),
            None,
        )
        terminal_moves = {"park", "close", "invite"}
        if (
            move
            and move not in terminal_moves
            and thread is not None
            and thread.status is ThreadStatus.ACTIVE
        ):
            ttl_s = (
                self._agenda_policy.config.ttl_seconds[GoalKind.CONTINUE_THREAD]
                if self._agenda_policy is not None else 300
            )
            priority = (
                self._agenda_policy.config.priorities[GoalKind.CONTINUE_THREAD]
                if self._agenda_policy is not None else active.priority
            )
            successor = Goal(
                goal_id=f"goal:continue_thread:{event.event_id}:next",
                kind=GoalKind.CONTINUE_THREAD,
                status=GoalStatus.CANDIDATE,
                priority=priority,
                reason="finish the delivered parent thread before switching topic",
                source=GoalSource.RULE,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_s),
                success_conditions=(
                    "parent thread reaches delivered park, close, or wait boundary",
                ),
                parent_thread_id=parent_id,
                metadata=_bound({
                    "source_event_id": event.event_id,
                    "summary": thread.summary,
                    "source_delivered": True,
                    "boundary_successor": True,
                    "relevant": True,
                }, self.limits.metadata_text_max_chars),
            )
            self._activate(successor)
            self._record("successor", GoalKind.CONTINUE_THREAD.value)
            return
        self._activate_next(now)

    def _complete_or_advance(
        self, goal_id: str, *, reason: str, outcome_id: str | None,
    ) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        goal = self._find(goal_id)
        intention = self._intentions.get(goal_id)
        if goal is None or intention is None:
            self._record("rejected", "unknown_goal")
            self._record_intention("rejected", "unknown_goal")
            return False
        if intention.step_index + 1 < intention.step_count:
            self._complete_intention(
                intention, reason_code=reason, outcome_id=outcome_id,
            )
            next_index = intention.step_index + 1
            next_intention = self._new_intention(
                goal,
                step_index=next_index,
                status=(
                    ShortIntentionStatus.ACTIVE
                    if self._snapshot.active is not None
                    and self._snapshot.active.goal_id == goal_id
                    else ShortIntentionStatus.PROPOSED
                ),
                reason_code="step_advanced",
                now=now,
            )
            self._intentions[goal_id] = next_intention
            self._record_intention("advanced", goal.kind.value)
            if next_intention.status is ShortIntentionStatus.ACTIVE:
                self._record_intention("activated", goal.kind.value)
            return True
        return self._terminal(
            goal_id, GoalStatus.COMPLETED, reason, outcome_id=outcome_id,
        )

    def _terminal(
        self,
        goal_id: str,
        status: GoalStatus,
        reason: str,
        *,
        outcome_id: str | None = None,
    ) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        goal = self._find(goal_id)
        if goal is None:
            self._record("rejected", "unknown_goal")
            return False
        clean_reason = _compact(reason, self.limits.metadata_text_max_chars)
        if not clean_reason:
            self._record("rejected", "invalid_reason")
            return False
        terminal = goal.with_status(status, suspend_reason=clean_reason)
        intention_status = {
            GoalStatus.COMPLETED: ShortIntentionStatus.COMPLETED,
            GoalStatus.FAILED: ShortIntentionStatus.FAILED,
            GoalStatus.CANCELLED: ShortIntentionStatus.CANCELLED,
            GoalStatus.EXPIRED: ShortIntentionStatus.CANCELLED,
        }.get(status)
        if intention_status is not None:
            self._terminal_intention_for_goal(
                goal_id,
                intention_status,
                reason_code=clean_reason,
                outcome_id=outcome_id,
            )
        self._remove(goal_id)
        self._intentions.pop(goal_id, None)
        self._append_terminal(terminal)
        self._record(status.value, goal.kind.value)
        self._activate_next(now)
        return True

    def _prune(self, now: datetime) -> None:
        expired: list[Goal] = []
        active = self._snapshot.active
        if active is not None and active.expires_at <= now:
            expired.append(active.with_status(GoalStatus.EXPIRED, suspend_reason="ttl"))
            self._terminal_intention_for_goal(
                active.goal_id, ShortIntentionStatus.CANCELLED, reason_code="ttl_expired",
            )
            self._intentions.pop(active.goal_id, None)
            active = None
        candidates = []
        for goal in self._snapshot.candidates:
            if goal.expires_at <= now:
                expired.append(goal.with_status(GoalStatus.EXPIRED, suspend_reason="ttl"))
                self._terminal_intention_for_goal(
                    goal.goal_id, ShortIntentionStatus.CANCELLED, reason_code="ttl_expired",
                )
                self._intentions.pop(goal.goal_id, None)
            else:
                candidates.append(goal)
        suspended = []
        for goal in self._snapshot.suspended:
            if goal.expires_at <= now or goal.metadata.get("relevant") is False:
                expired.append(goal.with_status(GoalStatus.EXPIRED, suspend_reason="ttl_or_irrelevant"))
                self._terminal_intention_for_goal(
                    goal.goal_id,
                    ShortIntentionStatus.CANCELLED,
                    reason_code="ttl_expired" if goal.expires_at <= now else "irrelevant",
                )
                self._intentions.pop(goal.goal_id, None)
            else:
                suspended.append(goal)
        if not expired:
            return
        self._snapshot = replace(
            self._snapshot, active=active, candidates=tuple(candidates), suspended=tuple(suspended),
        )
        for goal in expired:
            self._append_terminal(goal)
            self._record("expired", goal.kind.value)
        self._activate_next(now)

    def _activate_next(self, now: datetime) -> None:
        if self._snapshot.active is not None:
            return
        eligible = [
            goal for goal in (*self._snapshot.candidates, *self._snapshot.suspended)
            if goal.expires_at > now and goal.metadata.get("relevant") is not False
        ]
        if not eligible:
            self._notify_active(None)
            return
        next_goal = self._rank(eligible)[0]
        self._remove(next_goal.goal_id)
        self._activate(next_goal)

    def _activate(self, goal: Goal) -> None:
        active = goal.with_status(GoalStatus.ACTIVE, suspend_reason=None)
        self._snapshot = replace(self._snapshot, active=active)
        if goal.goal_id not in self._intentions:
            self._propose_intention(goal)
        previous = self._intentions[goal.goal_id]
        resumed = previous.status is ShortIntentionStatus.SUSPENDED
        self._transition_intention(
            goal.goal_id, ShortIntentionStatus.ACTIVE,
            reason_code="resumed" if resumed else "activated",
        )
        self._record("activated", active.kind.value)
        self._record_intention("resumed" if resumed else "activated", active.kind.value)
        self._notify_active(active.goal_id)

    def _remove(self, goal_id: str) -> None:
        active = self._snapshot.active
        self._snapshot = replace(
            self._snapshot,
            active=None if active and active.goal_id == goal_id else active,
            candidates=tuple(g for g in self._snapshot.candidates if g.goal_id != goal_id),
            suspended=tuple(g for g in self._snapshot.suspended if g.goal_id != goal_id),
        )

    def _find(self, goal_id: str) -> Goal | None:
        for goal in (
            *((self._snapshot.active,) if self._snapshot.active else ()),
            *self._snapshot.candidates,
            *self._snapshot.suspended,
        ):
            if goal.goal_id == goal_id:
                return goal
        return None

    def _all_goals(self) -> tuple[Goal, ...]:
        return (
            *((self._snapshot.active,) if self._snapshot.active else ()),
            *self._snapshot.candidates,
            *self._snapshot.suspended,
        )

    def _replace_goal(self, goal: Goal) -> None:
        active = self._snapshot.active
        self._snapshot = replace(
            self._snapshot,
            active=(
                goal if active is not None and active.goal_id == goal.goal_id else active
            ),
            candidates=tuple(
                goal if item.goal_id == goal.goal_id else item
                for item in self._snapshot.candidates
            ),
            suspended=tuple(
                goal if item.goal_id == goal.goal_id else item
                for item in self._snapshot.suspended
            ),
        )

    def _bounded_goal(self, goal: Goal) -> Goal:
        limit = self.limits.metadata_text_max_chars
        return replace(
            goal,
            reason=_compact(goal.reason, limit),
            success_conditions=tuple(_compact(item, limit) for item in goal.success_conditions),
            steps=tuple(_compact(item, limit) for item in goal.steps),
            suspend_reason=(
                _compact(goal.suspend_reason, limit) if goal.suspend_reason is not None else None
            ),
            metadata=_bound(goal.metadata, limit),
        )

    def _new_intention(
        self,
        goal: Goal,
        *,
        step_index: int,
        status: ShortIntentionStatus,
        reason_code: str,
        now: datetime,
    ) -> ShortIntention:
        return ShortIntention(
            intention_id=f"intention:{goal.goal_id}:{step_index + 1}",
            goal_id=goal.goal_id,
            status=status,
            step_index=step_index,
            step_count=len(goal.steps),
            step=goal.steps[step_index],
            created_at=now,
            updated_at=now,
            expires_at=goal.expires_at,
            reason_code=_compact(reason_code, self.limits.metadata_text_max_chars),
        )

    def _propose_intention(self, goal: Goal) -> None:
        if goal.goal_id in self._intentions:
            return
        intention = self._new_intention(
            goal,
            step_index=0,
            status=ShortIntentionStatus.PROPOSED,
            reason_code="goal_accepted",
            now=_utc(self._clock()),
        )
        self._intentions[goal.goal_id] = intention
        self._record_intention("proposed", goal.kind.value)

    def _current_intention(self) -> ShortIntention | None:
        active = self._snapshot.active
        if active is None:
            return None
        intention = self._intentions.get(active.goal_id)
        if intention is None or intention.status is not ShortIntentionStatus.ACTIVE:
            return None
        return intention

    def _transition_intention(
        self,
        goal_id: str,
        status: ShortIntentionStatus,
        *,
        reason_code: str,
    ) -> ShortIntention | None:
        intention = self._intentions.get(goal_id)
        if intention is None:
            return None
        updated = replace(
            intention,
            status=status,
            updated_at=_utc(self._clock()),
            reason_code=_compact(reason_code, self.limits.metadata_text_max_chars),
        )
        self._intentions[goal_id] = updated
        return updated

    def _complete_intention(
        self,
        intention: ShortIntention,
        *,
        reason_code: str,
        outcome_id: str | None,
    ) -> None:
        outcomes = intention.applied_outcome_ids
        if outcome_id is not None:
            outcomes = (*outcomes, outcome_id)
        terminal = replace(
            intention,
            status=ShortIntentionStatus.COMPLETED,
            updated_at=_utc(self._clock()),
            reason_code=_compact(reason_code, self.limits.metadata_text_max_chars),
            applied_outcome_ids=outcomes,
        )
        self._append_terminal_intention(terminal)
        self._record_intention("completed", reason_code)

    def _terminal_intention_for_goal(
        self,
        goal_id: str,
        status: ShortIntentionStatus,
        *,
        reason_code: str,
        outcome_id: str | None = None,
    ) -> None:
        intention = self._intentions.get(goal_id)
        if intention is None:
            return
        outcomes = intention.applied_outcome_ids
        if outcome_id is not None:
            outcomes = (*outcomes, outcome_id)
        terminal = replace(
            intention,
            status=status,
            updated_at=_utc(self._clock()),
            reason_code=_compact(reason_code, self.limits.metadata_text_max_chars),
            applied_outcome_ids=outcomes,
        )
        self._append_terminal_intention(terminal)
        self._record_intention(status.value, reason_code)

    def _append_terminal_intention(self, intention: ShortIntention) -> None:
        self._recent_intentions = (
            *self._recent_intentions,
            intention,
        )[-self.limits.terminal_history_max:]

    def _terminalize_dropped(self, goals: tuple[Goal, ...], *, reason: str) -> None:
        for goal in goals:
            self._remove(goal.goal_id)
            self._terminal_intention_for_goal(
                goal.goal_id,
                ShortIntentionStatus.CANCELLED,
                reason_code=reason,
            )
            self._intentions.pop(goal.goal_id, None)
            self._append_terminal(goal.with_status(
                GoalStatus.CANCELLED, suspend_reason=reason,
            ))
            self._record("cancelled", reason)

    def _record_intention(self, outcome: str, reason: str) -> None:
        self._intention_counts[outcome] = self._intention_counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_intention_event"):
            try:
                self._metrics.record_intention_event(
                    outcome, _intention_metric_reason(reason),
                )
            except Exception:
                pass

    def _append_terminal(self, goal: Goal) -> None:
        terminal = (*self._snapshot.recent_terminal, goal)[-self.limits.terminal_history_max:]
        self._snapshot = replace(self._snapshot, recent_terminal=terminal)

    @staticmethod
    def _rank(goals: Any) -> tuple[Goal, ...]:
        return tuple(sorted(goals, key=lambda g: (-g.priority, g.created_at, g.goal_id)))

    def _record(self, outcome: str, reason: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_goal_event"):
            try:
                self._metrics.record_goal_event(outcome, reason)
            except Exception:
                pass

    def _notify_active(self, goal_id: str | None) -> None:
        if self._on_active_changed is not None:
            try:
                self._on_active_changed(goal_id)
            except Exception:
                pass

    def _operator_audit(self, action: str, goal_id: str, reason: str) -> None:
        self._record("operator_override", action)
        if self._audit_sink is None:
            return
        now = _utc(self._clock())
        try:
            self._audit_seq += 1
            self._audit_sink(GroundedEvent(
                event_id=f"agent:goal_audit:{self._audit_seq:012d}:{uuid.uuid4().hex}",
                kind=AgentEventKind.GOAL_AUDIT,
                source=AgentEventSource.OPERATOR,
                timestamp=now,
                confidence=1.0,
                payload={
                    "action": action,
                    "goal_id": goal_id,
                    "reason": _compact(reason, self.limits.metadata_text_max_chars),
                },
                provenance=EventProvenance(producer="goal_manager_operator"),
            ))
        except Exception:
            pass


def _bound(value: Any, max_chars: int) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _bound(item, max_chars) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_bound(item, max_chars) for item in value)
    if isinstance(value, str) and len(value) > max_chars:
        return value[: max(1, max_chars - 1)].rstrip() + "…"
    return value


def _compact(value: Any, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _intention_metric_reason(reason: str) -> str:
    allowed = {
        *(kind.value for kind in GoalKind),
        "candidate_capacity",
        "duplicate_action_outcome",
        "execution_cancelled",
        "execution_failed",
        "invalid_action_outcome",
        "irrelevant",
        "not_delivered",
        "parent_thread_missing",
        "speech_intention_mismatch",
        "stale_action_outcome",
        "step_advanced",
        "superseded_by_delivered_move",
        "suspended_capacity",
        "ttl_expired",
        "unknown_goal",
        "unsupported_action_outcome",
        "verified",
        "verified_delivery",
    }
    if reason in allowed:
        return reason
    if reason.startswith("speech_completed:"):
        return "verified_delivery"
    if reason.startswith("preempted_by:"):
        return "preempted"
    return "other"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("goal clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)
