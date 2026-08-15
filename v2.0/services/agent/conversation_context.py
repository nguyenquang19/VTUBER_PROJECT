"""Bounded grounded context composer and Phase 12 context selector."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from interfaces.agent import ContextSelectorService, ConversationContextService
from interfaces.base import HealthStatus
from interfaces.memory import MemoryEntry
from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot, GroundedEvent

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

    @classmethod
    def from_loader(cls, loader: Any) -> "ConversationContextConfig":
        prefix = "context."
        selector = loader.get("conversation", "context_selector", {}) or {}
        if not isinstance(selector, Mapping):
            raise ValueError("context_selector must be a mapping")
        value = cls(
            max_chars=int(loader.get("conversation", prefix + "max_chars", 1400)),
            evidence_items=int(loader.get("conversation", prefix + "evidence_items", 3)),
            item_max_chars=int(loader.get("conversation", prefix + "item_max_chars", 220)),
            selector_max_chars=int(selector.get("max_chars", 1800)),
            memory_items=int(selector.get("memory_items", 3)),
            world_items=int(selector.get("world_items", 4)),
            capability_items=int(selector.get("capability_items", 3)),
        )
        if value.max_chars < 400 or value.evidence_items != 3 or value.item_max_chars <= 0:
            raise ValueError("conversation context needs max_chars>=400 and exactly 3 evidence items")
        if min(value.selector_max_chars, value.memory_items, value.world_items, value.capability_items) <= 0:
            raise ValueError("context selector limits must be positive")
        return value


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
        selector_enabled: bool = False,
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
        self._selector_enabled = bool(selector_enabled)
        self._running = False
        self._renders = 0
        self._last_chars = 0
        self._selector_renders = 0
        self._memory_items_total = 0
        self._world_items_total = 0
        self._memory_errors = 0
        self._world_overrides = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "ConversationContextComposer":
        return cls(ConversationContextConfig.from_loader(loader), **kwargs)

    @property
    def selector_enabled(self) -> bool:
        return self._selector_enabled

    def set_selector_enabled(self, enabled: bool) -> None:
        self._selector_enabled = bool(enabled)

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
        world = self._safe_snapshot(self._world_snapshot_provider)
        self_snapshot = self._safe_snapshot(self._self_snapshot_provider)
        capabilities = self._safe_snapshot(self._capability_snapshot_provider)
        memory = await self._query_memory(query, viewer_id)
        world_lines, world_paths = self._world_lines(world)
        memory_lines = self._memory_lines(memory, world_paths)
        capability_lines = self._capability_lines(capabilities)
        self_lines = self._self_lines(self_snapshot)
        self._selector_renders += 1
        self._world_items_total += len(world_lines)
        self._memory_items_total += len(memory_lines)
        return self._render(
            state,
            query,
            viewer_id=viewer_id,
            selector_lines=(*world_lines, *self_lines, *capability_lines, *memory_lines),
        )

    def _render(
        self,
        state: AgentStateSnapshot,
        query: str,
        *,
        viewer_id: str | None,
        selector_lines: tuple[str, ...] = (),
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
        max_chars = self.config.selector_max_chars if selector_lines else self.config.max_chars
        context = _fit_lines(lines, max_chars)
        self._renders += 1
        self._last_chars = len(context)
        if self._metrics is not None and hasattr(self._metrics, "observe_context_chars"):
            try:
                self._metrics.observe_context_chars(len(context))
            except Exception:
                pass
        return context

    async def _query_memory(self, query: str, viewer_id: str | None) -> tuple[MemoryEntry, ...]:
        memory = self._safe_snapshot(self._memory_provider)
        if memory is None or not hasattr(memory, "query"):
            return ()
        try:
            entries = await memory.query(query, top_k=self.config.memory_items, viewer_id=viewer_id)
            return tuple(item for item in entries if isinstance(item, MemoryEntry))[:self.config.memory_items]
        except Exception:
            self._memory_errors += 1
            return ()

    @staticmethod
    def _safe_snapshot(provider: Callable[[], Any] | None) -> Any:
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _world_lines(self, snapshot: Any) -> tuple[tuple[str, ...], frozenset[str]]:
        lines: list[str] = []
        paths: set[str] = set()
        for domain in _WORLD_DOMAINS:
            values = getattr(snapshot, domain, {}) if snapshot is not None else {}
            if not isinstance(values, Mapping):
                continue
            for key in sorted(values):
                if len(lines) >= self.config.world_items:
                    return tuple(lines), frozenset(paths)
                state = values[key]
                path = f"{domain}.{key}"
                paths.add(path)
                source = _text(getattr(state, "source", None), "unknown")
                confidence = _text(getattr(state, "confidence", None), "unknown")
                evidence = ",".join(getattr(state, "evidence_refs", ()) or ()) or "none"
                updated = _timestamp(getattr(state, "updated_at", None))
                value = _compact(_text(getattr(state, "value", None), "none"), self.config.item_max_chars)
                lines.append(f"Current world [{path}; source={source}; confidence={confidence}; updated_at={updated}; evidence={evidence}]: {value}")
        return tuple(lines), frozenset(paths)

    def _self_lines(self, snapshot: Any) -> tuple[str, ...]:
        if snapshot is None:
            return ()
        values = [
            f"busy={bool(getattr(snapshot, 'busy', False))}",
            f"degraded={bool(getattr(snapshot, 'degraded', False))}",
        ]
        for key in ("current_topic", "active_goal_id", "focused_thread_id"):
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
            outcome = "success" if status in {"success", "delivered"} else status
            provenance = _text(metadata.get("provenance"), "memory")
            confidence = _text(metadata.get("confidence"), "unknown")
            lines.append(
                f"Past memory [{entry.entry_id}; tier={entry.tier.value}; provenance={provenance}; "
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
        return f"Active goal [{goal.goal_id}; kind={goal.kind.value}]: {_compact(goal.reason, self.config.item_max_chars)}"

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


def _fit_lines(lines: list[str], max_chars: int) -> str:
    result: list[str] = []
    remaining = max_chars
    for line in lines:
        separator = 1 if result else 0
        if remaining <= separator:
            break
        fitted = line[: remaining - separator]
        result.append(fitted)
        remaining -= len(fitted) + separator
    return "\n".join(result)