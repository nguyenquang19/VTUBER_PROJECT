"""Exact compatibility text projections owned by canonical cognition."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.agent import ContextSelectorService, ConversationContextService
from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry
from interfaces.state import GoalSnapshot
from interfaces.state import AgentStateSnapshot, GroundedEvent

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WORLD_DOMAINS = ("stream", "social", "call", "media", "physical", "game")


@dataclass(frozen=True)
class ConversationContextConfig:
    max_chars: int = 1400
    evidence_items: int = 3
    item_max_chars: int = 220
    selector_max_chars: int = 1800
    memory_items: int = 3
    world_items: int = 4
    capability_items: int = 3

    def __post_init__(self) -> None:
        for name in (
            "max_chars", "evidence_items", "item_max_chars", "selector_max_chars",
            "memory_items", "world_items", "capability_items",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"conversation.{name} must be a positive integer")
        if self.max_chars < 400 or self.evidence_items != 3:
            raise ValueError("conversation context needs max_chars>=400 and exactly 3 evidence items")
        if self.selector_max_chars < self.max_chars:
            raise ValueError("context selector max_chars must cover compatibility context")

    @classmethod
    def from_loader(cls, loader: Any) -> "ConversationContextConfig":
        context = loader.get("cognition", "conversation_context_projection", {}) or {}
        selector = loader.get("cognition", "context_selector_projection", {}) or {}
        if not isinstance(context, Mapping):
            raise ValueError("conversation_context_projection must be a mapping")
        if not isinstance(selector, Mapping):
            raise ValueError("context_selector_projection must be a mapping")
        context_expected = {"max_chars", "evidence_items", "item_max_chars"}
        if set(context) != context_expected:
            raise ValueError("conversation context projection keys must match the canonical inventory")
        expected = {"max_chars", "memory_items", "world_items", "capability_items"}
        if set(selector) != expected:
            raise ValueError("context_selector keys must match the canonical inventory")
        return cls(
            max_chars=context.get("max_chars"),
            evidence_items=context.get("evidence_items"),
            item_max_chars=context.get("item_max_chars"),
            selector_max_chars=selector.get("max_chars"),
            memory_items=selector.get("memory_items"),
            world_items=selector.get("world_items"),
            capability_items=selector.get("capability_items"),
        )


class ConversationContextComposer(ConversationContextService, ContextSelectorService):
    service_id = "conversation_context"

    def __init__(
        self,
        config: ConversationContextConfig,
        *,
        goal_provider: Callable[[], GoalSnapshot] | None = None,
        metrics: Any = None,
        repair_policy: Any = None,
        relationship_context: Any = None,
        world_snapshot_provider: Callable[[], Any] | None = None,
        self_snapshot_provider: Callable[[], Any] | None = None,
        capability_snapshot_provider: Callable[[], Any] | None = None,
        memory_provider: Callable[[], Any] | None = None,
        operator_constraints_provider: Callable[[], Any] | None = None,
        selector_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._goal_provider = goal_provider
        self._metrics = metrics
        self._repair_policy = repair_policy
        self._relationship_context = relationship_context
        self._world_snapshot_provider = world_snapshot_provider
        self._self_snapshot_provider = self_snapshot_provider
        self._capability_snapshot_provider = capability_snapshot_provider
        self._memory_provider = memory_provider
        self._operator_constraints_provider = operator_constraints_provider
        if not isinstance(selector_enabled, bool):
            raise ValueError("selector_enabled must be boolean")
        self._selector_enabled = selector_enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._renders = 0
        self._last_chars = 0
        self._selector_renders = 0
        self._memory_items_total = 0
        self._world_items_total = 0
        self._memory_errors = 0
        self._world_overrides = 0
        self._source_errors = 0
        self._items_dropped = 0
        self._truncations = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "ConversationContextComposer":
        return cls(ConversationContextConfig.from_loader(loader), **kwargs)

    @property
    def selector_enabled(self) -> bool:
        return self._selector_enabled

    def set_selector_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("context selector enabled state must be boolean")
        self._selector_enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, renders=self._renders, selector_enabled=self._selector_enabled)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "conversation_context_renders_total": self._renders,
            "conversation_context_last_chars": self._last_chars,
            "conversation_context_selector_renders_total": self._selector_renders,
            "conversation_context_selector_memory_items_total": self._memory_items_total,
            "conversation_context_selector_world_items_total": self._world_items_total,
            "conversation_context_selector_memory_errors_total": self._memory_errors,
            "conversation_context_selector_world_override_total": self._world_overrides,
            "conversation_context_selector_source_errors_total": self._source_errors,
            "conversation_context_selector_items_dropped_total": self._items_dropped,
            "conversation_context_selector_truncations_total": self._truncations,
        }

    def render(
        self, state: AgentStateSnapshot, query: str = "", viewer_id: str | None = None,
    ) -> str:
        """Compatibility renderer used whenever the selector feature is disabled."""
        return self._render(state, query, viewer_id=viewer_id)

    async def select(
        self, state: AgentStateSnapshot, query: str = "", viewer_id: str | None = None,
    ) -> str:
        """Read bounded public snapshots and memory without changing any domain state."""
        if not self._selector_enabled:
            return self._render(state, query, viewer_id=viewer_id)
        operator = self._source_snapshot(self._operator_constraints_provider)
        world = self._source_snapshot(self._world_snapshot_provider)
        self_snapshot = self._source_snapshot(self._self_snapshot_provider)
        capabilities = self._source_snapshot(self._capability_snapshot_provider)
        memory = await self._query_memory(query, viewer_id)
        world_lines, world_paths = self._world_lines(world)
        memory_lines = self._memory_lines(memory, world_paths)
        capability_lines = self._capability_lines(capabilities)
        self_lines = self._self_lines(self_snapshot)
        operator_lines = self._operator_lines(operator)
        self._selector_renders += 1
        self._world_items_total += len(world_lines)
        self._memory_items_total += len(memory_lines)
        return self._render(
            state,
            query,
            viewer_id=viewer_id,
            selector_lines=(*operator_lines, *world_lines, *self_lines, *capability_lines),
            memory_lines=memory_lines,
        )

    def _render(
        self,
        state: AgentStateSnapshot,
        query: str,
        *,
        viewer_id: str | None,
        selector_lines: tuple[str, ...] = (),
        memory_lines: tuple[str, ...] = (),
    ) -> str:
        goals = self._safe_goals()
        lines = ["[Conversation continuity — grounded facts only; repair instead of guessing]"]
        lines.extend(selector_lines)
        lines.extend((self._topic_line(state), self._thread_line(state), self._goal_line(goals)))
        if self._relationship_context is not None:
            try:
                social = self._relationship_context.render_context(viewer_id)
            except Exception:
                social = ""
            if social:
                lines.append(social)
        repair = None
        if self._repair_policy is not None:
            try:
                repair = self._repair_policy.decide(state, query)
            except Exception:
                repair = None
        if repair is not None:
            evidence_ids = ",".join(repair.evidence_ids) or "none"
            lines.append(f"Repair policy [{repair.kind.value}; evidence={evidence_ids}]: {repair.instruction}")
        evidence = self._select_evidence(state.recent_events, query)
        for index in range(self.config.evidence_items):
            lines.append(self._evidence_line(index + 1, evidence[index]) if index < len(evidence) else f"Evidence {index + 1}: none recorded")
        if state.session_recap and state.session_recap.items:
            recap = " | ".join(f"{item.source_event_id}: {item.summary}" for item in state.session_recap.items[-2:])
            lines.append(f"Bounded recap: {_compact(recap, self.config.item_max_chars * 2)}")
        lines.extend(memory_lines)
        max_chars = self.config.selector_max_chars if selector_lines else self.config.max_chars
        context, dropped = _fit_lines(lines, max_chars)
        if dropped:
            self._truncations += 1
            self._items_dropped += dropped
        self._renders += 1
        self._last_chars = len(context)
        if self._metrics is not None and hasattr(self._metrics, "observe_context_chars"):
            try:
                self._metrics.observe_context_chars(len(context))
            except Exception:
                pass
        return context

    async def _query_memory(self, query: str, viewer_id: str | None) -> tuple[MemoryEntry, ...]:
        memory = self._source_snapshot(self._memory_provider)
        if memory is None or not hasattr(memory, "query"):
            return ()
        try:
            entries = await memory.query(query, top_k=self.config.memory_items, viewer_id=viewer_id)
            return tuple(item for item in entries if isinstance(item, MemoryEntry))[:self.config.memory_items]
        except Exception:
            self._memory_errors += 1
            return ()

    def _source_snapshot(self, provider: Callable[[], Any] | None) -> Any:
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            self._source_errors += 1
            return None

    def _world_lines(self, snapshot: Any) -> tuple[tuple[str, ...], frozenset[str]]:
        lines: list[str] = []
        paths: set[str] = set()
        now = _utc(self._clock())
        for domain in _WORLD_DOMAINS:
            values = getattr(snapshot, domain, {}) if snapshot is not None else {}
            if not isinstance(values, Mapping):
                continue
            for key in sorted(values):
                state = values[key]
                path = f"{domain}.{key}"
                expires_at = getattr(state, "expires_at", None)
                if expires_at is not None:
                    try:
                        if _utc(expires_at) <= now:
                            continue
                    except ValueError:
                        self._source_errors += 1
                        continue
                paths.add(path)
                if len(lines) >= self.config.world_items:
                    continue
                source = _text(getattr(state, "source", None), "unknown")
                confidence = _text(getattr(state, "confidence", None), "unknown")
                evidence = ",".join(getattr(state, "evidence_refs", ()) or ()) or "none"
                updated = _timestamp(getattr(state, "updated_at", None))
                value = _compact(_text(getattr(state, "value", None), "none"), self.config.item_max_chars)
                lines.append(f"Current world [{path}; source={source}; confidence={confidence}; updated_at={updated}; evidence={evidence}]: {value}")
        return tuple(lines), frozenset(paths)

    def _operator_lines(self, snapshot: Any) -> tuple[str, ...]:
        if snapshot is None:
            return ()
        if not isinstance(snapshot, Mapping):
            self._source_errors += 1
            return ()
        paused = snapshot.get("paused")
        emergency = snapshot.get("emergency")
        if not isinstance(paused, bool) or not isinstance(emergency, bool):
            self._source_errors += 1
            return ()
        reason = snapshot.get("reason")
        clean_reason = _compact(reason, self.config.item_max_chars) if isinstance(reason, str) else "none"
        return (f"Operator constraints [paused={paused}; emergency={emergency}; reason={clean_reason}]",)

    def _self_lines(self, snapshot: Any) -> tuple[str, ...]:
        if snapshot is None:
            return ()
        values = [
            f"busy={bool(getattr(snapshot, 'busy', False))}",
            f"degraded={bool(getattr(snapshot, 'degraded', False))}",
        ]
        for key in (
            "current_topic", "active_goal_id", "current_intention_id", "focused_thread_id",
        ):
            value = getattr(snapshot, key, None)
            if value:
                values.append(f"{key}={_compact(str(value), self.config.item_max_chars)}")
        return ("Self state [" + "; ".join(values) + "]",)

    def _capability_lines(self, snapshot: Any) -> tuple[str, ...]:
        if not isinstance(snapshot, Mapping):
            return ()
        lines: list[str] = []
        for entry in snapshot.get("capabilities", ()):
            if not isinstance(entry, Mapping) or len(lines) >= self.config.capability_items:
                continue
            capability = entry.get("capability", {})
            availability = entry.get("availability", {})
            if not isinstance(capability, Mapping) or not isinstance(availability, Mapping) or not availability.get("available"):
                continue
            identifier = _text(capability.get("capability_id"), "unknown")
            action_type = _text(capability.get("action_type"), "unknown")
            evidence = ",".join(availability.get("evidence_refs", ()) or ()) or "none"
            lines.append(f"Available capability [{identifier}; action={action_type}; evidence={evidence}]")
        return tuple(lines)

    def _memory_lines(self, entries: tuple[MemoryEntry, ...], world_paths: frozenset[str]) -> tuple[str, ...]:
        lines: list[str] = []
        for entry in entries:
            metadata = entry.metadata if isinstance(entry.metadata, Mapping) else {}
            world_path = str(metadata.get("world_path") or "").strip()
            if world_path and world_path in world_paths:
                self._world_overrides += 1
                continue
            status = str(metadata.get("action_status") or "unknown").strip().casefold()
            if status in {"success", "succeeded", "delivered"} and metadata.get("verified") is not True:
                self._source_errors += 1
                continue
            outcome = "success" if status in {"success", "succeeded", "delivered"} else status
            provenance = _text(metadata.get("provenance"), "memory")
            confidence = _text(metadata.get("confidence"), "unknown")
            lines.append(
                f"Past memory (past evidence, never current truth) [{entry.entry_id}; tier={entry.tier.value}; provenance={provenance}; "
                f"confidence={confidence}; outcome={outcome}; timestamp={_timestamp(entry.timestamp)}]: "
                f"{_compact(entry.content, self.config.item_max_chars)}"
            )
        return tuple(lines)

    def _safe_goals(self) -> GoalSnapshot:
        if self._goal_provider is None:
            return GoalSnapshot()
        try:
            return self._goal_provider()
        except Exception:
            return GoalSnapshot()

    def _topic_line(self, state: AgentStateSnapshot) -> str:
        if state.current_topic is None:
            return "Current topic: none recorded"
        topic = state.current_topic
        return f"Current topic [{topic.source_event_id}]: {_compact(topic.summary, self.config.item_max_chars)}"

    def _thread_line(self, state: AgentStateSnapshot) -> str:
        if not state.open_threads:
            return "Open thread: none recorded"
        thread = max(state.open_threads, key=lambda item: (item.updated_at, item.thread_id))
        evidence_ids = ",".join(item.source_event_id for item in thread.evidence) or "legacy"
        parts = [
            f"Open thread [{thread.thread_id}; kind={thread.kind.value}; status={thread.status.value}; evidence={evidence_ids}]",
            f"summary={_compact(thread.summary, self.config.item_max_chars)}",
            f"next_move={thread.next_move.value if thread.next_move else 'none'}",
        ]
        if thread.claims:
            parts.append("already_said=" + _compact(" | ".join(item.text for item in thread.claims[-2:]), self.config.item_max_chars))
        if thread.viewer_contributions:
            parts.append("viewer_input=" + _compact(" | ".join(item.text for item in thread.viewer_contributions[-2:]), self.config.item_max_chars))
        if thread.open_questions:
            parts.append("open_question=" + _compact(thread.open_questions[-1].text, self.config.item_max_chars))
        return "; ".join(parts)

    def _goal_line(self, goals: GoalSnapshot) -> str:
        if goals.active is None:
            return "Active goal: none recorded"
        goal = goals.active
        intention = goals.current_intention
        intention_text = (
            f"; intention={intention.intention_id}; step={intention.step_index + 1}/{intention.step_count}"
            if intention is not None and intention.goal_id == goal.goal_id else "; intention=none"
        )
        return f"Active goal [{goal.goal_id}; kind={goal.kind.value}{intention_text}]: {_compact(goal.reason, self.config.item_max_chars)}"

    def _select_evidence(self, events: tuple[GroundedEvent, ...], query: str) -> tuple[GroundedEvent, ...]:
        query_terms = _terms(query)
        ranked = sorted(events, key=lambda event: (len(query_terms & _terms(_event_detail(event))) * 5, event.timestamp, event.event_id), reverse=True)[: self.config.evidence_items]
        return tuple(ranked)

    def _evidence_line(self, index: int, event: GroundedEvent) -> str:
        source_id = event.provenance.source_event_id or event.event_id
        return f"Evidence {index} [{event.kind.value}; source_id={source_id}; producer={event.provenance.producer}]: {_compact(_event_detail(event), self.config.item_max_chars)}"


def _event_detail(event: GroundedEvent) -> str:
    text = event.payload.get("text")
    return str(text) if text else "; ".join(f"{key}={value}" for key, value in sorted(event.payload.items()))


def _terms(value: str) -> set[str]:
    return {item.casefold() for item in _WORD_RE.findall(value) if len(item) > 1}


def _compact(value: str, max_chars: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"


def _text(value: Any, default: str) -> str:
    return str(value).strip() if value is not None and str(value).strip() else default


def _timestamp(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else "unknown"


def _fit_lines(lines: list[str], max_chars: int) -> tuple[str, int]:
    result: list[str] = []
    remaining = max_chars
    dropped = 0
    for index, line in enumerate(lines):
        separator = 1 if result else 0
        if remaining <= separator or len(line) > remaining - separator:
            dropped += len(lines) - index
            break
        result.append(line)
        remaining -= len(line) + separator
    return "\n".join(result), dropped


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("context selector time must be timezone-aware")
    return value.astimezone(timezone.utc)
