"""Bounded deterministic memory recall policy.

The gate never derives hints from stored content. It only maps trusted memory kinds to
fixed instructions, so enabled failure cannot expose a transcript or stored fact.
"""
from __future__ import annotations

import math
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry, RecallDecision, RecallGateService
from services.memory.config import MemoryRuntimeConfig


_HINTS = {
    "EPISODIC": (
        "Use prior session context only as a subtle continuity cue; "
        "do not quote or retell stored memory."
    ),
    "PREFERENCE": (
        "Adapt subtly to a known preference when relevant; "
        "do not state or quote the stored memory."
    ),
    "RELATIONSHIP_NOTE": (
        "Use prior social context only to warm the tone; "
        "do not recite a profile or stored fact."
    ),
    "RELATIONSHIP_CALLBACK": (
        "An approved recurring callback is available; use only a light indirect callback "
        "if it fits, and never quote stored wording."
    ),
    "SELF_SUMMARY": (
        "Use prior self-context only as a subtle consistency cue; "
        "do not quote stored memory."
    ),
}
_GENERIC_HINT = (
    "A relevant past context exists; use it only as a subtle cue "
    "and do not quote or retell it."
)


class RecallGate(RecallGateService):
    service_id = "recall_gate"

    def __init__(
        self, config: MemoryRuntimeConfig, *, enabled: bool = True,
    ) -> None:
        if not isinstance(config, MemoryRuntimeConfig):
            raise ValueError("config must be MemoryRuntimeConfig")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        self._config = config
        self._enabled = enabled
        self._running = False
        self._surface_times: deque[datetime] = deque(
            maxlen=config.recall_frequency_cap,
        )
        self._entry_times: OrderedDict[str, datetime] = OrderedDict()
        self._evaluated = 0
        self._surfaced = 0
        self._disabled = 0
        self._failures = 0
        self._suppressed: dict[str, int] = {}
        self._history_evictions = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._clear_state()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            enabled=self._enabled,
            retained_entries=len(self._entry_times),
            retained_surfaces=len(self._surface_times),
        )

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self._clear_state()

    def evaluate(
        self, entries: tuple[MemoryEntry, ...], *, now: datetime,
    ) -> tuple[RecallDecision, ...]:
        if not isinstance(entries, tuple):
            raise ValueError("recall entries must be a tuple")
        if len(entries) > self._config.max_query_top_k:
            raise ValueError("recall entries exceed configured query bound")
        current = _utc(now)
        if not all(isinstance(entry, MemoryEntry) for entry in entries):
            self._failures += 1
            raise ValueError("recall entries must contain MemoryEntry values")
        self._prune(current)
        decisions: list[RecallDecision] = []
        surfaced_this_call = 0
        for entry in entries:
            self._evaluated += 1
            salience = _salience(entry)
            reason = self._suppression_reason(
                entry, salience, current, surfaced_this_call,
            )
            if reason is not None:
                self._suppressed[reason] = self._suppressed.get(reason, 0) + 1
                if reason == "disabled":
                    self._disabled += 1
                decisions.append(RecallDecision(
                    memory_ref=entry.entry_id,
                    surface=False,
                    salience=salience,
                    latent_hint=None,
                    reason_code=reason,
                ))
                continue
            hint = _HINTS.get(
                str(entry.metadata.get("cognitive_kind") or "").strip().upper(),
                _GENERIC_HINT,
            )
            decisions.append(RecallDecision(
                memory_ref=entry.entry_id,
                surface=True,
                salience=salience,
                latent_hint=hint,
                reason_code="surfaced",
            ))
            surfaced_this_call += 1
            self._surfaced += 1
            self._surface_times.append(current)
            self._remember_entry(entry.entry_id, current)
        return tuple(decisions)

    def get_metrics(self) -> dict[str, Any]:
        rate = self._surfaced / self._evaluated if self._evaluated else 0.0
        return {
            "recall_gate_running": self._running,
            "recall_gate_enabled": self._enabled,
            "recall_gate_evaluated_total": self._evaluated,
            "recall_gate_surfaced_total": self._surfaced,
            "recall_gate_disabled_total": self._disabled,
            "recall_gate_failures_total": self._failures,
            "recall_gate_suppressed": dict(sorted(self._suppressed.items())),
            "recall_gate_recall_rate": rate,
            "recall_gate_retained_entries": len(self._entry_times),
            "recall_gate_retained_surfaces": len(self._surface_times),
            "recall_gate_history_evictions_total": self._history_evictions,
        }

    def _suppression_reason(
        self,
        entry: MemoryEntry,
        salience: float,
        now: datetime,
        surfaced_this_call: int,
    ) -> str | None:
        if not self._enabled:
            return "disabled"
        if salience < self._config.recall_salience_threshold:
            return "salience"
        if surfaced_this_call >= self._config.recall_max_hints:
            return "context_cap"
        previous = self._entry_times.get(entry.entry_id)
        if previous is not None and (
            now - previous
        ).total_seconds() < self._config.recall_cooldown_s:
            return "cooldown"
        if len(self._surface_times) >= self._config.recall_frequency_cap:
            return "frequency_cap"
        return None

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._config.recall_frequency_window_s)
        while self._surface_times and self._surface_times[0] <= cutoff:
            self._surface_times.popleft()
        stale = [
            entry_id for entry_id, timestamp in self._entry_times.items()
            if (now - timestamp).total_seconds() >= self._config.recall_cooldown_s
        ]
        for entry_id in stale:
            self._entry_times.pop(entry_id, None)

    def _remember_entry(self, entry_id: str, now: datetime) -> None:
        self._entry_times[entry_id] = now
        self._entry_times.move_to_end(entry_id)
        while len(self._entry_times) > self._config.recall_entry_history_max:
            self._entry_times.popitem(last=False)
            self._history_evictions += 1

    def _clear_state(self) -> None:
        self._surface_times.clear()
        self._entry_times.clear()


def _salience(entry: MemoryEntry) -> float:
    value = entry.metadata.get("summary_salience")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return entry.importance
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return entry.importance
    return max(entry.importance, number)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("recall time must be timezone-aware")
    return value.astimezone(timezone.utc)
