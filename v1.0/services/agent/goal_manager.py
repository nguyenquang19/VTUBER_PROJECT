"""Deterministic single-active GoalManager state machine (Master Plan M2.1)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.agent import GoalManagerService
from interfaces.base import HealthStatus
from services.agent.goal_types import Goal, GoalSnapshot, GoalStatus


@dataclass(frozen=True)
class GoalLimits:
    candidates_max: int
    suspended_max: int
    terminal_history_max: int
    metadata_text_max_chars: int

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
    ) -> None:
        self.limits = limits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._on_active_changed = on_active_changed
        self._snapshot = GoalSnapshot()
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
        on_active_changed: Callable[[str | None], None] | None = None,
    ) -> "GoalManager":
        return cls(
            GoalLimits.from_loader(loader), clock=clock, metrics=metrics,
            on_active_changed=on_active_changed,
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


def _bound(value: Any, max_chars: int) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _bound(item, max_chars) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_bound(item, max_chars) for item in value]
    if isinstance(value, str) and len(value) > max_chars:
        return value[: max(1, max_chars - 1)].rstrip() + "…"
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
