"""Bounded grounded prompt context composer for conversation continuity (M4.4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.agent import ConversationContextService
from interfaces.base import HealthStatus
from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot, GroundedEvent

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class ConversationContextConfig:
    max_chars: int = 1400
    evidence_items: int = 3
    item_max_chars: int = 220

    @classmethod
    def from_loader(cls, loader: Any) -> "ConversationContextConfig":
        prefix = "context."
        value = cls(
            max_chars=int(loader.get("conversation", prefix + "max_chars", 1400)),
            evidence_items=int(
                loader.get("conversation", prefix + "evidence_items", 3)
            ),
            item_max_chars=int(
                loader.get("conversation", prefix + "item_max_chars", 220)
            ),
        )
        if value.max_chars < 400 or value.evidence_items != 3 or value.item_max_chars <= 0:
            raise ValueError("conversation context needs max_chars>=400 and exactly 3 evidence items")
        return value


class ConversationContextComposer(ConversationContextService):
    service_id = "conversation_context"

    def __init__(
        self,
        config: ConversationContextConfig,
        *,
        goal_provider: Callable[[], GoalSnapshot] | None = None,
        metrics: Any = None,
        repair_policy: Any = None,
        relationship_context: Any = None,
    ) -> None:
        self.config = config
        self._goal_provider = goal_provider
        self._metrics = metrics
        self._repair_policy = repair_policy
        self._relationship_context = relationship_context
        self._running = False
        self._renders = 0
        self._last_chars = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, goal_provider: Callable[[], GoalSnapshot] | None = None,
        metrics: Any = None,
        repair_policy: Any = None,
        relationship_context: Any = None,
    ) -> "ConversationContextComposer":
        return cls(
            ConversationContextConfig.from_loader(loader),
            goal_provider=goal_provider, metrics=metrics, repair_policy=repair_policy,
            relationship_context=relationship_context,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, renders=self._renders)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "conversation_context_renders_total": self._renders,
            "conversation_context_last_chars": self._last_chars,
        }

    def render(
        self, state: AgentStateSnapshot, query: str = "", viewer_id: str | None = None,
    ) -> str:
        goals = self._safe_goals()
        lines = [
            "[Conversation continuity — grounded facts only; repair instead of guessing]",
            self._topic_line(state),
            self._thread_line(state),
            self._goal_line(goals),
        ]
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
            lines.append(
                f"Repair policy [{repair.kind.value}; evidence={evidence_ids}]: "
                f"{repair.instruction}"
            )
        evidence = self._select_evidence(state.recent_events, query)
        for index in range(self.config.evidence_items):
            if index < len(evidence):
                lines.append(self._evidence_line(index + 1, evidence[index]))
            else:
                lines.append(f"Evidence {index + 1}: none recorded")
        if state.session_recap and state.session_recap.items:
            recap = " | ".join(
                f"{item.source_event_id}: {item.summary}"
                for item in state.session_recap.items[-2:]
            )
            lines.append(f"Bounded recap: {_compact(recap, self.config.item_max_chars * 2)}")
        context = _fit_lines(lines, self.config.max_chars)
        self._renders += 1
        self._last_chars = len(context)
        if self._metrics is not None and hasattr(self._metrics, "observe_context_chars"):
            try:
                self._metrics.observe_context_chars(len(context))
            except Exception:
                pass
        return context

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
        return (
            f"Current topic [{topic.source_event_id}]: "
            f"{_compact(topic.summary, self.config.item_max_chars)}"
        )

    def _thread_line(self, state: AgentStateSnapshot) -> str:
        if not state.open_threads:
            return "Open thread: none recorded"
        thread = max(state.open_threads, key=lambda item: (item.updated_at, item.thread_id))
        evidence_ids = ",".join(item.source_event_id for item in thread.evidence) or "legacy"
        return (
            f"Open thread [{thread.thread_id}; kind={thread.kind.value}; evidence={evidence_ids}]: "
            f"{_compact(thread.summary, self.config.item_max_chars)}"
        )

    def _goal_line(self, goals: GoalSnapshot) -> str:
        if goals.active is None:
            return "Active goal: none recorded"
        goal = goals.active
        return (
            f"Active goal [{goal.goal_id}; kind={goal.kind.value}]: "
            f"{_compact(goal.reason, self.config.item_max_chars)}"
        )

    def _select_evidence(
        self, events: tuple[GroundedEvent, ...], query: str,
    ) -> tuple[GroundedEvent, ...]:
        query_terms = _terms(query)
        ranked = sorted(
            events,
            key=lambda event: (
                len(query_terms & _terms(_event_detail(event))) * 5,
                event.timestamp,
                event.event_id,
            ),
            reverse=True,
        )[: self.config.evidence_items]
        return tuple(ranked)

    def _evidence_line(self, index: int, event: GroundedEvent) -> str:
        source_id = event.provenance.source_event_id or event.event_id
        return (
            f"Evidence {index} [{event.kind.value}; source_id={source_id}; "
            f"producer={event.provenance.producer}]: "
            f"{_compact(_event_detail(event), self.config.item_max_chars)}"
        )


def _event_detail(event: GroundedEvent) -> str:
    text = event.payload.get("text")
    if text:
        return str(text)
    return "; ".join(f"{key}={value}" for key, value in sorted(event.payload.items()))


def _terms(value: str) -> set[str]:
    return {item.casefold() for item in _WORD_RE.findall(value) if len(item) > 1}


def _compact(value: str, max_chars: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"


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
