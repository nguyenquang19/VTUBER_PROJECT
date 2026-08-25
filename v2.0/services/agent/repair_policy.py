"""Deterministic ambiguity, missing-evidence, and conflict repair policy (M4.5)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from interfaces.agent import ConversationRepairService
from interfaces.base import HealthStatus
from interfaces.state import AgentEventKind, AgentStateSnapshot, GroundedEvent

_REFERENCE_RE = re.compile(
    r"\b(nãy cậu (?:bảo|nói)|lúc nãy cậu (?:bảo|nói)|cậu vừa (?:bảo|nói)|"
    r"you (?:said|told me) earlier)\b",
    re.IGNORECASE,
)
_WHO_RE = re.compile(r"\b(ai nói vậy|ai bảo vậy|who said that)\b", re.IGNORECASE)
_AMBIGUOUS_RE = re.compile(
    r"\b(cái đó|chuyện đó|việc đó|nó|that one|that story)\b", re.IGNORECASE,
)
_NAME_RE = re.compile(r"\b(?:tôi|tớ|mình)\s+tên\s+([\wÀ-ỹ-]{1,40})", re.IGNORECASE)


class RepairKind(str, Enum):
    AMBIGUITY = "ambiguity"
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class RepairDecision:
    kind: RepairKind
    instruction: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairPolicyConfig:
    ambiguity_max_candidates: int = 2
    conflict_window_seconds: float = 1800.0

    @classmethod
    def from_loader(cls, loader: Any) -> "RepairPolicyConfig":
        prefix = "repair."
        value = cls(
            ambiguity_max_candidates=int(
                loader.get("conversation", prefix + "ambiguity_max_candidates", 2)
            ),
            conflict_window_seconds=float(
                loader.get("conversation", prefix + "conflict_window_seconds", 1800)
            ),
        )
        if min(value.ambiguity_max_candidates, value.conflict_window_seconds) <= 0:
            raise ValueError("repair limits must be positive")
        return value


class ConversationRepairPolicy(ConversationRepairService):
    service_id = "conversation_repair"

    def __init__(
        self,
        config: RepairPolicyConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
    ) -> "ConversationRepairPolicy":
        return cls(RepairPolicyConfig.from_loader(loader), clock=clock, metrics=metrics)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, repairs=sum(self._counts.values()))

    def get_metrics(self) -> dict[str, Any]:
        return {f"conversation_repair_{key}_total": value for key, value in self._counts.items()}

    def decide(self, state: AgentStateSnapshot, query: str) -> RepairDecision | None:
        text = " ".join(str(query or "").split())
        if not text:
            return None
        events = self._recent(state.recent_events)
        conflict = self._conflict(events)
        if conflict is not None and _query_mentions_fact(text, conflict[0]):
            return self._record(RepairDecision(
                RepairKind.CONFLICT,
                "Recorded facts conflict. Do not choose or assert either value; say the records disagree and ask for confirmation.",
                conflict[1],
            ))
        chat_events = tuple(event for event in events if event.kind is AgentEventKind.CHAT_RECEIVED)
        if _WHO_RE.search(text) and len(chat_events) != 1:
            return self._record(RepairDecision(
                RepairKind.AMBIGUITY,
                "The speaker reference is ambiguous. Ask one concise clarifying question instead of naming a viewer.",
                tuple(event.event_id for event in chat_events[-3:]),
            ))
        if _AMBIGUOUS_RE.search(text) and len(state.open_threads) > self.config.ambiguity_max_candidates:
            return self._record(RepairDecision(
                RepairKind.AMBIGUITY,
                "Multiple grounded threads could match. Ask which thread the viewer means.",
                tuple(thread.thread_id for thread in state.open_threads[-3:]),
            ))
        if _REFERENCE_RE.search(text):
            matching = _matching_events(events, text)
            if not matching:
                return self._record(RepairDecision(
                    RepairKind.MISSING_EVIDENCE,
                    "No matching recorded statement exists. Say you are not certain and ask the viewer to repeat or provide evidence.",
                ))
        return None

    def _recent(self, events: tuple[GroundedEvent, ...]) -> tuple[GroundedEvent, ...]:
        cutoff = _utc(self._clock()) - timedelta(seconds=self.config.conflict_window_seconds)
        return tuple(event for event in events if event.timestamp >= cutoff)

    def _conflict(
        self, events: tuple[GroundedEvent, ...],
    ) -> tuple[str, tuple[str, ...]] | None:
        facts: dict[tuple[str, str], dict[str, list[str]]] = {}
        for event in events:
            if event.kind is not AgentEventKind.CHAT_RECEIVED:
                continue
            subject = str(
                event.payload.get("viewer_alias") or event.payload.get("viewer_id") or "viewer"
            )
            key = str(event.payload.get("fact_key") or "")
            value = str(event.payload.get("fact_value") or "")
            if not key or not value:
                match = _NAME_RE.search(str(event.payload.get("text") or ""))
                if match:
                    key, value = "name", match.group(1)
            if key and value:
                facts.setdefault((subject, key.casefold()), {}).setdefault(
                    value.casefold(), [],
                ).append(event.event_id)
        for (_subject, fact_key), values in facts.items():
            if len(values) > 1:
                return fact_key, tuple(event_id for ids in values.values() for event_id in ids)
        return None

    def _record(self, decision: RepairDecision) -> RepairDecision:
        key = decision.kind.value
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_repair"):
            try:
                self._metrics.record_repair(key)
            except Exception:
                pass
        return decision


def _matching_events(
    events: tuple[GroundedEvent, ...], query: str,
) -> tuple[GroundedEvent, ...]:
    stop = {
        "nãy", "cậu", "bảo", "nói", "lúc", "vừa", "gì", "rằng",
        "đúng", "không", "đang", "phải", "nhỉ", "à",
    }
    terms = {
        item.casefold() for item in re.findall(r"\w+", query)
        if len(item) > 1 and item.casefold() not in stop
    }
    if not terms:
        return ()
    return tuple(
        event for event in events
        if terms & {item.casefold() for item in re.findall(r"\w+", str(event.payload.get("text") or ""))}
    )


def _query_mentions_fact(query: str, fact_key: str) -> bool:
    lowered = query.casefold()
    aliases = {"name": ("name", "tên")}
    return any(term in lowered for term in aliases.get(fact_key, (fact_key,)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
