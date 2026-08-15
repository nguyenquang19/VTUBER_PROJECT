"""Deterministic single-active GoalManager state machine (Master Plan M2.1)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from interfaces.agent import GoalManagerService
from interfaces.base import HealthStatus
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
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
    proposal_allowed_kinds: tuple[GoalKind, ...] = (
        GoalKind.CONTINUE_THREAD, GoalKind.WAIT_FOR_CHAT_ANSWER,
    )

    @classmethod
    def from_loader(cls, loader: Any) -> "GoalLimits":
        prefix = "goal_manager."
        return cls(
            candidates_max=int(loader.get("agent_goals", prefix + "candidates_max", 16)),
            suspended_max=int(loader.get("agent_goals", prefix + "suspended_max", 8)),
            terminal_history_max=int(
                loader.get("agent_goals", prefix + "terminal_history_max", 32)
            ),
            metadata_text_max_chars=int(
                loader.get("agent_goals", prefix + "metadata_text_max_chars", 240)
            ),
            operator_priority=int(
                loader.get("agent_goals", "priorities.operator_pinned", 90)
            ),
            operator_ttl_s=int(
                loader.get("agent_goals", prefix + "operator_pinned_ttl_s", 3600)
            ),
            proposal_allowed_kinds=tuple(
                GoalKind(str(value))
                for value in loader.get(
                    "agent_goals", "proposal.allowed_kinds",
                    [GoalKind.CONTINUE_THREAD.value, GoalKind.WAIT_FOR_CHAT_ANSWER.value],
                )
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
        self._running = False
        self._counts: dict[str, int] = {}
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
        if self._metrics is not None and hasattr(self._metrics, "set_goal_active_age"):
            try:
                self._metrics.set_goal_active_age(age)
            except Exception:
                pass
        return {
            **{f"goal_{name}_total": count for name, count in sorted(self._counts.items())},
            "goal_active_age_seconds": age,
            "goal_candidates": len(self._snapshot.candidates),
            "goal_suspended": len(self._snapshot.suspended),
        }

    def submit(self, goal: Goal) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        if goal.status is not GoalStatus.CANDIDATE or goal.expires_at <= now:
            self._record("rejected", goal.kind.value)
            return False
        if self._find(goal.goal_id) is not None:
            self._record("rejected", "duplicate")
            return False
        goal = replace(goal, metadata=_bound(goal.metadata, self.limits.metadata_text_max_chars))
        self._record("created", goal.kind.value)
        active = self._snapshot.active
        if active is None:
            self._activate(goal)
            return True
        if goal.priority > active.priority:
            suspended = active.with_status(
                GoalStatus.SUSPENDED, suspend_reason=f"preempted_by:{goal.goal_id}",
            )
            self._snapshot = replace(
                self._snapshot,
                active=None,
                suspended=self._bounded_suspended((*self._snapshot.suspended, suspended)),
            )
            self._record("preempted", active.kind.value)
            self._activate(goal)
            return True
        candidates = self._rank((*self._snapshot.candidates, goal))[: self.limits.candidates_max]
        accepted = any(item.goal_id == goal.goal_id for item in candidates)
        self._snapshot = replace(self._snapshot, candidates=candidates)
        if not accepted:
            self._record("rejected", "candidate_cap")
        return accepted

    def complete(self, goal_id: str, *, reason: str = "success") -> bool:
        return self._terminal(goal_id, GoalStatus.COMPLETED, reason)

    def cancel(self, goal_id: str, *, reason: str) -> bool:
        return self._terminal(goal_id, GoalStatus.CANCELLED, reason)

    def snapshot(self) -> GoalSnapshot:
        self._prune(_utc(self._clock()))
        return GoalSnapshot(
            active=self._snapshot.active,
            candidates=self._snapshot.candidates,
            suspended=self._snapshot.suspended,
            recent_terminal=self._snapshot.recent_terminal,
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
        if not self.complete(goal_id, reason=_compact(reason, self.limits.metadata_text_max_chars)):
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
        action = str(event.payload.get("action") or "")
        allowed = {
            GoalKind.ACK_DONATION: {"ack_donation"},
            GoalKind.ANSWER_FOLLOW_UP: {"read_chat", "follow_up"},
            GoalKind.CONTINUE_THREAD: {"continue_thread"},
        }
        if action == "continue_thread" and active.kind is GoalKind.CONTINUE_THREAD:
            self._complete_continue_turn(active, event, state)
            return
        if action in allowed.get(active.kind, set()):
            self.complete(active.goal_id, reason=f"speech_completed:{event.event_id}")
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
        self, active: Goal, event: GroundedEvent, state: AgentStateSnapshot,
    ) -> None:
        """Commit one delivered move and atomically retain its parent boundary."""
        now = _utc(self._clock())
        parent_id = active.parent_thread_id
        duplicates = tuple(
            goal for goal in self._all_goals()
            if (
                goal.kind is GoalKind.CONTINUE_THREAD
                and goal.parent_thread_id == parent_id
                and goal.goal_id != active.goal_id
            )
        )
        self._remove(active.goal_id)
        self._append_terminal(active.with_status(
            GoalStatus.COMPLETED,
            suspend_reason=f"speech_completed:{event.event_id}",
        ))
        self._record("completed", active.kind.value)
        for goal in duplicates:
            self._remove(goal.goal_id)
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

    def _terminal(self, goal_id: str, status: GoalStatus, reason: str) -> bool:
        now = _utc(self._clock())
        self._prune(now)
        goal = self._find(goal_id)
        if goal is None:
            self._record("rejected", "unknown_goal")
            return False
        terminal = goal.with_status(status, suspend_reason=reason)
        self._remove(goal_id)
        self._append_terminal(terminal)
        self._record(status.value, goal.kind.value)
        self._activate_next(now)
        return True

    def _prune(self, now: datetime) -> None:
        expired: list[Goal] = []
        active = self._snapshot.active
        if active is not None and active.expires_at <= now:
            expired.append(active.with_status(GoalStatus.EXPIRED, suspend_reason="ttl"))
            active = None
        candidates = []
        for goal in self._snapshot.candidates:
            if goal.expires_at <= now:
                expired.append(goal.with_status(GoalStatus.EXPIRED, suspend_reason="ttl"))
            else:
                candidates.append(goal)
        suspended = []
        for goal in self._snapshot.suspended:
            if goal.expires_at <= now or goal.metadata.get("relevant") is False:
                expired.append(goal.with_status(GoalStatus.EXPIRED, suspend_reason="ttl_or_irrelevant"))
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
        self._record("activated", active.kind.value)
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

    def _append_terminal(self, goal: Goal) -> None:
        terminal = (*self._snapshot.recent_terminal, goal)[-self.limits.terminal_history_max:]
        self._snapshot = replace(self._snapshot, recent_terminal=terminal)

    def _bounded_suspended(self, goals: tuple[Goal, ...]) -> tuple[Goal, ...]:
        return self._rank(goals)[: self.limits.suspended_max]

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
    if isinstance(value, (list, tuple, set)):
        return [_bound(item, max_chars) for item in value]
    if isinstance(value, str) and len(value) > max_chars:
        return value[: max(1, max_chars - 1)].rstrip() + "…"
    return value


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
