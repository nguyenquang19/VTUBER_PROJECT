"""Deterministic read-only Cognitive Context and Focus shadow projection."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveContext,
    CognitiveContextBuilderService,
    CognitiveContextRequest,
    CognitiveConversationState,
    CognitiveEvidenceItem,
    CognitiveEvidenceSource,
    CognitiveHardState,
    CognitiveMemoryItem,
    CognitiveMode,
    CognitiveSpeechSummary,
    FocusOrigin,
    FocusState,
    MemoryKind,
    MemoryScope,
)
from interfaces.compatibility import SelfSnapshot, StateValue, WorldSnapshot
from interfaces.memory import MemoryEntry
from services.agent.goal_types import GoalSnapshot
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    GroundedEvent,
    OpenThread,
)
from services.data.sanitize import mask_pii


_WORLD_DOMAINS = ("stream", "social", "call", "media", "physical", "game")
_EVIDENCE_PRIORITY = {
    CognitiveEvidenceSource.CHAT: 0,
    CognitiveEvidenceSource.THREAD: 1,
    CognitiveEvidenceSource.GOAL: 2,
    CognitiveEvidenceSource.WORLD: 3,
    CognitiveEvidenceSource.SELF: 4,
    CognitiveEvidenceSource.ENVIRONMENT: 5,
    CognitiveEvidenceSource.OPERATOR: 6,
}
_SPEECH_MODE = {
    "read_chat": "read_chat",
    "self_talk": "self_talk",
    "follow_up": "follow_up",
    "ask_follow_up": "follow_up",
    "continue_thread": "follow_up",
}
_SENSITIVE_AVAILABILITY_PREFIXES = ("permission:", "executor:", "verifier:")


class CognitiveContextBuilder(CognitiveContextBuilderService):
    """Build typed snapshots without consuming proposals or mutating sources."""

    service_id = "cognitive_context_builder"

    def __init__(
        self,
        config: CognitionConfig,
        *,
        world_model: Any,
        self_model: Any,
        capability_registry: Any,
        agent_state: Any,
        goal_manager: Any = None,
        thread_manager: Any = None,
        memory_service: Any = None,
        metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, CognitionConfig):
            raise ValueError("config must be CognitionConfig")
        for name, value in (
            ("world_model", world_model),
            ("self_model", self_model),
            ("capability_registry", capability_registry),
            ("agent_state", agent_state),
        ):
            if value is None or not callable(getattr(value, "snapshot", None)):
                raise ValueError(f"{name} must provide snapshot()")
        self._config = config
        self._world_model = world_model
        self._self_model = self_model
        self._capability_registry = capability_registry
        self._agent_state = agent_state
        self._goal_manager = goal_manager
        self._thread_manager = thread_manager
        self._memory_service = memory_service
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._contexts: deque[CognitiveContext] = deque()
        self._focus: FocusState | None = None
        self._running = False
        self._counts: dict[str, int] = {}
        self._source_counts: dict[tuple[str, str], int] = {}
        self._evicted: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "CognitiveContextBuilder":
        return cls(CognitionConfig.from_mapping(loader.section("cognition")), **kwargs)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._contexts.clear()
        self._focus = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            retained_contexts=len(self._contexts),
            focus_present=self._focus is not None,
        )

    async def build(
        self, request: CognitiveContextRequest,
    ) -> CognitiveContext | None:
        started = time.perf_counter()
        if not self._running or not isinstance(request, CognitiveContextRequest):
            self._record_build("rejected")
            self._observe_duration(started)
            return None
        try:
            now = _utc(self._clock(), "clock")
            self._validate_request_time(request.requested_at, now)
        except (TypeError, ValueError):
            self._record_source("hard_state", "failed")
            self._record_build("rejected")
            self._observe_duration(started)
            return None

        self._record_source("hard_state", "accepted")
        try:
            world = self._world_model.snapshot()
            self._validate_world_snapshot(world, request.requested_at)
        except Exception:
            self._record_source("world", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None
        self._record_source("world", "accepted")
        try:
            self_state = self._self_model.snapshot()
            self._validate_self_snapshot(self_state, request.requested_at)
        except Exception:
            self._record_source("self", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None
        self._record_source("self", "accepted")
        try:
            capabilities = self._capability_registry.snapshot()
            capability_id = self._capability_snapshot_id(capabilities, request.requested_at)
        except Exception:
            self._record_source("capability", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None
        self._record_source("capability", "accepted")
        try:
            agent = self._agent_state.snapshot()
            if not isinstance(agent, AgentStateSnapshot):
                raise ValueError("agent snapshot is invalid")
        except Exception:
            self._record_source("agent_state", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None
        self._record_source("agent_state", "accepted")

        failures = set(request.hard_state.source_failure_codes)
        if self_state.degraded:
            failures.add("self")

        events = self._event_index(agent)
        if events is None:
            self._record_source("agent_state", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None

        chat = self._chat_digest(request, events)
        if request.trigger_event_ref is not None and chat is None:
            self._record_source("agent_state", "failed")
            self._record_build("unavailable")
            self._observe_duration(started)
            return None

        goal, goal_failed = self._goal_snapshot()
        threads, thread_failed = self._thread_snapshot()
        if goal_failed:
            failures.add("goal")
            self._record_source("goal", "failed")
        else:
            self._record_source("goal", "accepted")
        if thread_failed:
            failures.add("thread")
            self._record_source("thread", "failed")
        else:
            self._record_source("thread", "accepted")

        selected_thread = self._select_thread(
            threads, self_state.focused_thread_id, request.trigger_event_ref,
            request.requested_at,
        )
        focus, focus_outcome = self._project_focus(
            self_state, threads, events, request.requested_at,
        )
        self._record_focus(focus_outcome)
        if focus_outcome in {"mismatch", "invalid"}:
            failures.add("thread")

        attention, current_paths, attention_failed = self._attention_items(
            world, self_state, goal, selected_thread, request.requested_at,
        )
        failures.update(attention_failed)

        conversation = self._conversation_state(
            agent, self_state, goal, selected_thread, chat, attention,
        )
        if conversation is None:
            self._record_build("unavailable")
            self._observe_duration(started)
            return None

        recent_speech, delivery_failed = self._recent_speech(
            events, request.requested_at,
        )
        if delivery_failed:
            failures.add("delivery")
            self._record_source("delivery", "failed")
        elif recent_speech:
            self._record_source("delivery", "accepted")
        else:
            self._record_source("delivery", "omitted")

        memory, memory_failed = await self._memory_items(
            chat.summary if chat is not None else (conversation.topic or conversation.summary),
            current_paths,
            request.requested_at,
        )
        if memory_failed:
            failures.add("memory")
            self._record_source("memory", "failed")
        elif memory:
            self._record_source("memory", "accepted")
        else:
            self._record_source("memory", "omitted")

        try:
            hard_state = self._hard_state(request.hard_state, failures)
            modes = (
                (CognitiveMode.WAIT,)
                if _has_hold(hard_state)
                else (CognitiveMode.WAIT, CognitiveMode.SPEAK)
            )
            placeholder = CognitiveContext(
                config=self._config,
                schema_version=self._config.schema_version,
                context_id="0" * 64,
                created_at=request.requested_at,
                session_id=request.session_id,
                world_snapshot_id=world.snapshot_id,
                self_snapshot_id=self_state.snapshot_id,
                capability_snapshot_id=capability_id,
                focus_snapshot_id=focus.focus_id if focus is not None else None,
                operator_state=hard_state,
                available_modes=modes,
                available_actions=(),
                chat_digest=chat,
                attention_items=attention,
                conversation_state=conversation,
                memory_items=memory,
                recent_delivered_speech=recent_speech,
            )
            encoded = _canonical_json(placeholder, exclude={"context_id"})
            if len(encoded) > self._config.max_context_serialized_chars:
                raise ValueError("context exceeds total serialized bound")
            context_values = {
                field.name: getattr(placeholder, field.name)
                for field in fields(placeholder)
            }
            context_values["context_id"] = (
                f"ctx:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
            )
            context = CognitiveContext(config=self._config, **context_values)
        except (TypeError, ValueError):
            self._record_build("rejected")
            self._observe_duration(started)
            return None

        final_chars = len(_canonical_json(context))
        if final_chars > self._config.max_context_serialized_chars:
            self._record_build("rejected")
            self._observe_duration(started)
            return None
        self._retain(context, focus)
        outcome = "degraded" if failures else "ready"
        self._record_build(outcome)
        self._observe_chars(final_chars)
        self._observe_duration(started)
        return context

    def recent(self, limit: int | None = None) -> tuple[CognitiveContext, ...]:
        if limit is None:
            return tuple(self._contexts)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._config.max_context_snapshots
        ):
            raise ValueError("limit must be a positive bounded integer")
        return tuple(self._contexts)[-limit:]

    def focus_snapshot(self) -> FocusState | None:
        return self._focus

    def get_metrics(self) -> dict[str, Any]:
        return {
            "cognitive_context_builder_running": self._running,
            "cognitive_context_builder_retained": len(self._contexts),
            "cognitive_context_builder_focus_present": self._focus is not None,
            "cognitive_context_builder_builds": dict(sorted(self._counts.items())),
            "cognitive_context_builder_sources": {
                f"{source}:{outcome}": count
                for (source, outcome), count in sorted(self._source_counts.items())
            },
            "cognitive_context_builder_evicted": dict(sorted(self._evicted.items())),
        }

    def _validate_request_time(self, requested: datetime, now: datetime) -> None:
        requested = _utc(requested, "requested_at")
        if requested < now - timedelta(seconds=self._config.max_context_request_age_seconds):
            raise ValueError("context request is stale")
        if requested > now + timedelta(seconds=self._config.max_context_future_skew_seconds):
            raise ValueError("context request is from the future")

    def _validate_world_snapshot(self, world: Any, requested_at: datetime) -> None:
        if not isinstance(world, WorldSnapshot):
            raise ValueError("world snapshot is invalid")
        _bounded_id(world.snapshot_id, "world_snapshot_id", self._config)
        self._validate_source_time(world.created_at, requested_at, "world.created_at")

    def _validate_self_snapshot(self, self_state: Any, requested_at: datetime) -> None:
        if not isinstance(self_state, SelfSnapshot):
            raise ValueError("self snapshot is invalid")
        _bounded_id(self_state.snapshot_id, "self_snapshot_id", self._config)
        self._validate_source_time(self_state.created_at, requested_at, "self.created_at")

    def _validate_source_time(
        self, value: datetime, requested_at: datetime, name: str,
    ) -> None:
        timestamp = _utc(value, name)
        if timestamp > requested_at + timedelta(
            seconds=self._config.max_context_future_skew_seconds
        ):
            raise ValueError(f"{name} is from the future")
        if timestamp < requested_at - timedelta(
            seconds=self._config.max_context_request_age_seconds
        ):
            raise ValueError(f"{name} is stale")

    def _capability_snapshot_id(
        self, snapshot: Any, requested_at: datetime,
    ) -> str:
        if not isinstance(snapshot, Mapping) or set(snapshot) != {"enabled", "capabilities"}:
            raise ValueError("capability snapshot shape is invalid")
        if not isinstance(snapshot["enabled"], bool) or not isinstance(
            snapshot["capabilities"], list,
        ):
            raise ValueError("capability snapshot types are invalid")
        sanitized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in snapshot["capabilities"]:
            if not isinstance(item, Mapping):
                raise ValueError("capability entry is invalid")
            capability = item.get("capability")
            availability = item.get("availability")
            if not isinstance(capability, Mapping) or not isinstance(availability, Mapping):
                raise ValueError("capability entry is malformed")
            capability_id = _bounded_id(
                capability.get("capability_id"), "capability_id", self._config,
            )
            action_type = _bounded_label(
                capability.get("action_type"), "action_type", self._config,
            )
            if capability_id in seen or availability.get("capability_id") != capability_id:
                raise ValueError("capability identity collision")
            seen.add(capability_id)
            available = availability.get("available")
            if not isinstance(available, bool):
                raise ValueError("capability available must be bool")
            reason_code = _bounded_label(
                availability.get("reason_code"), "reason_code", self._config,
            )
            checked_at = _parse_time(availability.get("checked_at"), "checked_at")
            self._validate_source_time(checked_at, requested_at, "capability.checked_at")
            evidence = tuple(
                ref for ref in _safe_refs(
                    availability.get("evidence_refs"), self._config, allow_empty=True,
                )
                if not ref.casefold().startswith(_SENSITIVE_AVAILABILITY_PREFIXES)
            )
            sanitized.append({
                "capability_id": capability_id,
                "action_type": action_type,
                "available": available,
                "reason_code": reason_code,
                "checked_at": checked_at,
                "evidence_refs": evidence,
            })
        sanitized.sort(key=lambda value: (value["action_type"], value["capability_id"]))
        digest = hashlib.sha256(_canonical_json(sanitized).encode("utf-8")).hexdigest()
        return f"capctx:{digest}"

    def _event_index(
        self, snapshot: AgentStateSnapshot,
    ) -> dict[str, GroundedEvent] | None:
        result: dict[str, GroundedEvent] = {}
        for event in snapshot.recent_events:
            if not isinstance(event, GroundedEvent):
                return None
            existing = result.get(event.event_id)
            if existing is not None and _canonical_json(existing) != _canonical_json(event):
                return None
            result[event.event_id] = event
        return result

    def _chat_digest(
        self,
        request: CognitiveContextRequest,
        events: Mapping[str, GroundedEvent],
    ) -> CognitiveEvidenceItem | None:
        if request.trigger_event_ref is None:
            return None
        event = events.get(request.trigger_event_ref)
        if event is None or event.kind is not AgentEventKind.CHAT_RECEIVED:
            return None
        if event.provenance.session_id not in (None, request.session_id):
            return None
        if not _event_is_fresh(event, request.requested_at, self._config):
            return None
        text = _safe_text(event.payload.get("text"), self._config.max_text_chars)
        if text is None:
            return None
        refs = _event_refs(event, self._config)
        try:
            return CognitiveEvidenceItem(
                config=self._config,
                schema_version=self._config.schema_version,
                evidence_id=event.event_id,
                source=CognitiveEvidenceSource.CHAT,
                summary=text,
                provenance_refs=refs,
                observed_at=event.timestamp,
                expires_at=event.timestamp + timedelta(
                    seconds=self._config.max_recent_speech_age_seconds
                ),
            )
        except ValueError:
            return None

    def _goal_snapshot(self) -> tuple[GoalSnapshot | None, bool]:
        if self._goal_manager is None or not callable(
            getattr(self._goal_manager, "snapshot", None)
        ):
            return None, True
        try:
            value = self._goal_manager.snapshot()
        except Exception:
            return None, True
        return (value, False) if isinstance(value, GoalSnapshot) else (None, True)

    def _thread_snapshot(self) -> tuple[tuple[OpenThread, ...], bool]:
        if self._thread_manager is None or not callable(
            getattr(self._thread_manager, "snapshot", None)
        ):
            return (), True
        try:
            value = self._thread_manager.snapshot()
        except Exception:
            return (), True
        if not isinstance(value, tuple) or any(not isinstance(item, OpenThread) for item in value):
            return (), True
        identifiers = [item.thread_id for item in value]
        return (value, False) if len(identifiers) == len(set(identifiers)) else ((), True)

    def _select_thread(
        self,
        threads: tuple[OpenThread, ...],
        focused_id: str | None,
        trigger_ref: str | None,
        requested_at: datetime,
    ) -> OpenThread | None:
        fresh = tuple(item for item in threads if item.expires_at > requested_at)
        if trigger_ref is not None:
            matches = [
                item for item in fresh
                if any(evidence.source_event_id == trigger_ref for evidence in item.evidence)
                or item.origin_event_id == trigger_ref
            ]
            if matches:
                return max(matches, key=lambda item: (item.updated_at, item.thread_id))
        if focused_id is not None:
            match = [item for item in fresh if item.thread_id == focused_id]
            if len(match) == 1:
                return match[0]
        return max(fresh, key=lambda item: (item.updated_at, item.thread_id), default=None)

    def _attention_items(
        self,
        world: WorldSnapshot,
        self_state: SelfSnapshot,
        goal: GoalSnapshot | None,
        thread: OpenThread | None,
        requested_at: datetime,
    ) -> tuple[tuple[CognitiveEvidenceItem, ...], set[str], set[str]]:
        items: list[CognitiveEvidenceItem] = []
        failures: set[str] = set()
        current_paths: set[str] = set()
        for domain in _WORLD_DOMAINS:
            bucket = getattr(world, domain)
            for key, state in sorted(bucket.items()):
                path = f"{domain}.{key}"
                current_paths.add(path)
                if state.expires_at is not None and state.expires_at <= requested_at:
                    continue
                item = self._world_item(world, path, state)
                if item is None:
                    failures.add("world")
                else:
                    items.append(item)
        self_item = self._self_item(self_state)
        if self_item is None:
            failures.add("self")
        else:
            items.append(self_item)
        if goal is not None and goal.active is not None:
            goal_item = self._goal_item(goal)
            if goal_item is None:
                failures.add("goal")
            else:
                items.append(goal_item)
        if thread is not None:
            thread_item = self._thread_item(thread)
            if thread_item is None:
                failures.add("thread")
            else:
                items.append(thread_item)
        items.sort(key=lambda item: (
            _EVIDENCE_PRIORITY[item.source],
            -item.observed_at.timestamp(),
            item.evidence_id,
        ))
        deduped = _dedupe_contracts(items, "evidence_id")
        if deduped is None:
            failures.add("context")
            return (), current_paths, failures
        return (
            tuple(deduped[: self._config.max_attention_items]),
            current_paths,
            failures,
        )

    def _world_item(
        self, world: WorldSnapshot, path: str, state: StateValue,
    ) -> CognitiveEvidenceItem | None:
        try:
            summary = _safe_text(
                f"{path}={_canonical_json(state.value)}", self._config.max_text_chars,
            )
            if summary is None:
                return None
            refs = _safe_refs((world.snapshot_id, *state.evidence_refs), self._config)
            return CognitiveEvidenceItem(
                config=self._config,
                schema_version=self._config.schema_version,
                evidence_id=_derived_id("world", path),
                source=CognitiveEvidenceSource.WORLD,
                summary=summary,
                provenance_refs=refs,
                observed_at=state.updated_at,
                expires_at=state.expires_at,
            )
        except (TypeError, ValueError):
            return None

    def _self_item(self, snapshot: SelfSnapshot) -> CognitiveEvidenceItem | None:
        material = {
            "speaking": snapshot.speaking,
            "busy": snapshot.busy,
            "degraded": snapshot.degraded,
            "current_topic": snapshot.current_topic,
            "attention_target": snapshot.attention_target,
            "active_goal_id": snapshot.active_goal_id,
            "current_intention_id": snapshot.current_intention_id,
            "focused_thread_id": snapshot.focused_thread_id,
        }
        try:
            summary = _safe_text(_canonical_json(material), self._config.max_text_chars)
            if summary is None:
                return None
            return CognitiveEvidenceItem(
                config=self._config,
                schema_version=self._config.schema_version,
                evidence_id=_derived_id("self", snapshot.snapshot_id),
                source=CognitiveEvidenceSource.SELF,
                summary=summary,
                provenance_refs=(_bounded_id(
                    snapshot.snapshot_id, "self_snapshot_id", self._config,
                ),),
                observed_at=snapshot.created_at,
                expires_at=None,
            )
        except (TypeError, ValueError):
            return None

    def _goal_item(self, snapshot: GoalSnapshot) -> CognitiveEvidenceItem | None:
        goal = snapshot.active
        if goal is None or goal.expires_at <= goal.created_at:
            return None
        refs = [goal.goal_id]
        source_ref = goal.metadata.get("source_event_id")
        if isinstance(source_ref, str):
            refs.append(source_ref)
        try:
            return CognitiveEvidenceItem(
                config=self._config,
                schema_version=self._config.schema_version,
                evidence_id=_derived_id("goal", goal.goal_id),
                source=CognitiveEvidenceSource.GOAL,
                summary=_safe_text(goal.reason, self._config.max_text_chars) or "goal",
                provenance_refs=_safe_refs(tuple(refs), self._config),
                observed_at=goal.created_at,
                expires_at=goal.expires_at,
            )
        except ValueError:
            return None

    def _thread_item(self, thread: OpenThread) -> CognitiveEvidenceItem | None:
        refs = [thread.thread_id]
        if thread.origin_event_id is not None:
            refs.append(thread.origin_event_id)
        refs.extend(item.source_event_id for item in thread.evidence)
        try:
            return CognitiveEvidenceItem(
                config=self._config,
                schema_version=self._config.schema_version,
                evidence_id=_derived_id("thread", thread.thread_id),
                source=CognitiveEvidenceSource.THREAD,
                summary=_safe_text(thread.summary, self._config.max_text_chars) or thread.topic,
                provenance_refs=_safe_refs(tuple(refs), self._config),
                observed_at=thread.updated_at,
                expires_at=thread.expires_at,
            )
        except ValueError:
            return None

    def _conversation_state(
        self,
        agent: AgentStateSnapshot,
        self_state: SelfSnapshot,
        goal: GoalSnapshot | None,
        thread: OpenThread | None,
        chat: CognitiveEvidenceItem | None,
        attention: tuple[CognitiveEvidenceItem, ...],
    ) -> CognitiveConversationState | None:
        topic = thread.topic if thread is not None else self_state.current_topic
        if topic is None and agent.current_topic is not None:
            topic = agent.current_topic.summary
        active_goal = goal.active if goal is not None else None
        intention = goal.current_intention if goal is not None else None
        summary = thread.summary if thread is not None else None
        if summary is None and chat is not None:
            summary = chat.summary
        refs: list[str] = []
        if chat is not None:
            refs.append(chat.evidence_id)
        refs.extend(item.evidence_id for item in attention)
        try:
            return CognitiveConversationState(
                config=self._config,
                schema_version=self._config.schema_version,
                topic=_optional_safe_text(topic, self._config.max_text_chars),
                thread_ref=thread.thread_id if thread is not None else None,
                goal_ref=active_goal.goal_id if active_goal is not None else None,
                intention_ref=intention.intention_id if intention is not None else None,
                summary=_optional_safe_text(summary, self._config.max_text_chars),
                evidence_refs=_safe_refs(tuple(refs), self._config, allow_empty=True),
            )
        except ValueError:
            return None

    async def _memory_items(
        self,
        query: str | None,
        current_paths: set[str],
        requested_at: datetime,
    ) -> tuple[tuple[CognitiveMemoryItem, ...], bool]:
        if query is None or self._memory_service is None or not callable(
            getattr(self._memory_service, "query", None)
        ):
            return (), self._memory_service is None
        try:
            entries = await self._memory_service.query(
                query,
                top_k=self._config.memory_query_top_k,
                viewer_id=None,
            )
        except Exception:
            return (), True
        if not isinstance(entries, list):
            return (), True
        items: list[CognitiveMemoryItem] = []
        invalid = False
        for entry in entries:
            if not isinstance(entry, MemoryEntry):
                invalid = True
                continue
            try:
                item = self._memory_item(entry, current_paths, requested_at)
            except (TypeError, ValueError):
                invalid = True
                continue
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: (-item.observed_at.timestamp(), item.memory_ref))
        deduped = _dedupe_contracts(items, "memory_ref")
        if deduped is None:
            return (), True
        return tuple(deduped[: self._config.max_memory_items]), invalid

    def _memory_item(
        self,
        entry: MemoryEntry,
        current_paths: set[str],
        requested_at: datetime,
    ) -> CognitiveMemoryItem | None:
        metadata = entry.metadata
        path = metadata.get("world_path") or metadata.get("self_path")
        if isinstance(path, str) and path in current_paths:
            return None
        kind = MemoryKind(metadata.get("cognitive_kind"))
        scope = MemoryScope(metadata.get("cognitive_scope"))
        if scope is MemoryScope.VIEWER:
            return None
        if metadata.get("viewer_id") is not None:
            raise ValueError("non-viewer memory must not carry viewer_id")
        provenance = _safe_refs(metadata.get("provenance_refs"), self._config)
        confidence = metadata.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("memory confidence is invalid")
        expires = None
        if metadata.get("expires_at") is not None:
            expires = _parse_time(metadata.get("expires_at"), "memory.expires_at")
            if expires <= requested_at:
                return None
        return CognitiveMemoryItem(
            config=self._config,
            schema_version=self._config.schema_version,
            memory_ref=entry.entry_id,
            kind=kind,
            summary=_safe_text(entry.content, self._config.max_text_chars) or entry.content,
            scope=scope,
            viewer_ref=None,
            provenance_refs=provenance,
            observed_at=entry.timestamp,
            expires_at=expires,
            confidence=float(confidence),
        )

    def _recent_speech(
        self,
        events: Mapping[str, GroundedEvent],
        requested_at: datetime,
    ) -> tuple[tuple[CognitiveSpeechSummary, ...], bool]:
        items: list[CognitiveSpeechSummary] = []
        invalid = False
        for event in events.values():
            if event.kind is not AgentEventKind.SPEECH_COMPLETED:
                continue
            age = (requested_at - event.timestamp).total_seconds()
            if age < -self._config.max_context_future_skew_seconds:
                invalid = True
                continue
            if age > self._config.max_recent_speech_age_seconds:
                continue
            text = _safe_text(event.payload.get("text"), self._config.max_speech_chars)
            action = str(event.payload.get("action") or "").casefold()
            source_mode = _SPEECH_MODE.get(action)
            if text is None or source_mode not in self._config.speech_source_modes:
                continue
            delivery_id = event.provenance.source_event_id or event.event_id
            try:
                items.append(CognitiveSpeechSummary(
                    config=self._config,
                    schema_version=self._config.schema_version,
                    delivery_id=delivery_id,
                    speech_text=text,
                    delivered_at=event.timestamp,
                    source_mode=source_mode,
                    evidence_refs=_event_refs(event, self._config),
                ))
            except ValueError:
                invalid = True
        items.sort(key=lambda item: (-item.delivered_at.timestamp(), item.delivery_id))
        deduped = _dedupe_contracts(items, "delivery_id")
        if deduped is None:
            return (), True
        return tuple(deduped[: self._config.max_recent_delivered_speech]), invalid

    def _project_focus(
        self,
        self_state: SelfSnapshot,
        threads: tuple[OpenThread, ...],
        events: Mapping[str, GroundedEvent],
        requested_at: datetime,
    ) -> tuple[FocusState | None, str]:
        focused_id = self_state.focused_thread_id
        if focused_id is None:
            return None, "absent"
        matches = [item for item in threads if item.thread_id == focused_id]
        if len(matches) != 1:
            return None, "mismatch"
        thread = matches[0]
        if thread.expires_at <= requested_at:
            return None, "stale"
        origin_event = events.get(thread.origin_event_id or "")
        origin = _focus_origin(origin_event)
        if origin is None:
            return None, "invalid"
        expires_at = min(
            thread.expires_at,
            thread.created_at + timedelta(seconds=self._config.focus_ttl_seconds),
        )
        if not thread.created_at <= thread.updated_at < expires_at:
            return None, "stale"
        unresolved = tuple(
            clean for item in thread.open_questions
            if item.source_event_id in events
            for clean in (_safe_text(item.text, self._config.max_text_chars),)
            if clean is not None
        )
        claims = tuple(
            clean for item in thread.claims
            if item.source_event_id in events
            and events[item.source_event_id].kind is AgentEventKind.SPEECH_COMPLETED
            for clean in (_safe_text(item.text, self._config.max_text_chars),)
            if clean is not None
        )
        refs = [thread.thread_id, origin_event.event_id]
        refs.extend(
            item.source_event_id for item in (*thread.open_questions, *thread.claims)
            if item.source_event_id in events
        )
        try:
            payload = {
                "schema_version": self._config.schema_version,
                "topic": _safe_text(thread.topic, self._config.max_text_chars),
                "stance": None,
                "unresolved_items": tuple(dict.fromkeys(unresolved)),
                "claims_delivered": tuple(dict.fromkeys(claims)),
                "continuation_pressure": self._config.focus_pressure_by_status[
                    thread.status.value
                ],
                "saturation": min(
                    1.0,
                    thread.move_count / self._config.focus_saturation_move_count,
                ),
                "origin": origin,
                "evidence_refs": _safe_refs(tuple(refs), self._config),
                "born_at": thread.created_at,
                "updated_at": thread.updated_at,
                "expires_at": expires_at,
            }
            digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            focus = FocusState(
                config=self._config,
                focus_id=f"focus:{digest}",
                **payload,
            )
        except (KeyError, TypeError, ValueError):
            return None, "invalid"
        return focus, "present"

    def _hard_state(
        self, current: CognitiveHardState, failures: set[str],
    ) -> CognitiveHardState:
        return CognitiveHardState(
            config=self._config,
            schema_version=self._config.schema_version,
            emergency=current.emergency,
            operator_hold=current.operator_hold,
            safety_hold=current.safety_hold,
            permission_hold=current.permission_hold,
            transaction_conflict=current.transaction_conflict,
            critical_state=current.critical_state,
            source_failure_codes=tuple(sorted(failures)),
        )

    def _retain(self, context: CognitiveContext, focus: FocusState | None) -> None:
        if not self._contexts or self._contexts[-1].context_id != context.context_id:
            if len(self._contexts) >= self._config.max_context_snapshots:
                self._contexts.popleft()
                self._record_eviction("context")
            self._contexts.append(context)
        if self._focus is not None and (
            focus is None or focus.focus_id != self._focus.focus_id
        ):
            self._record_eviction("focus")
        self._focus = focus

    def _record_build(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        _call_metric(self._metrics, "record_cognitive_context_build", outcome)

    def _record_source(self, source: str, outcome: str) -> None:
        key = (source, outcome)
        self._source_counts[key] = self._source_counts.get(key, 0) + 1
        _call_metric(self._metrics, "record_cognitive_context_source", source, outcome)

    def _record_focus(self, outcome: str) -> None:
        _call_metric(self._metrics, "record_cognitive_focus_projection", outcome)

    def _record_eviction(self, kind: str) -> None:
        self._evicted[kind] = self._evicted.get(kind, 0) + 1
        _call_metric(self._metrics, "record_cognitive_snapshot_evicted", kind)

    def _observe_duration(self, started: float) -> None:
        _call_metric(
            self._metrics,
            "observe_cognitive_context_build_duration",
            max(0.0, time.perf_counter() - started),
        )

    def _observe_chars(self, chars: int) -> None:
        _call_metric(self._metrics, "observe_cognitive_context_serialized_chars", chars)


def _has_hold(state: CognitiveHardState) -> bool:
    return any((
        state.emergency,
        state.operator_hold,
        state.safety_hold,
        state.permission_hold,
        state.transaction_conflict,
        state.critical_state,
    ))


def _focus_origin(event: GroundedEvent | None) -> FocusOrigin | None:
    if event is None:
        return None
    if event.source is AgentEventSource.OPERATOR:
        return FocusOrigin.OPERATOR
    mapping = {
        AgentEventKind.CHAT_RECEIVED: FocusOrigin.CHAT,
        AgentEventKind.SPEECH_COMPLETED: FocusOrigin.SELF,
        AgentEventKind.SELF_TALK_COMPLETED: FocusOrigin.SELF,
        AgentEventKind.GOAL_AUDIT: FocusOrigin.GOAL,
        AgentEventKind.ENVIRONMENT_OBSERVED: FocusOrigin.ENVIRONMENT,
    }
    return mapping.get(event.kind)


def _event_is_fresh(
    event: GroundedEvent, requested_at: datetime, config: CognitionConfig,
) -> bool:
    age = (requested_at - event.timestamp).total_seconds()
    return -config.max_context_future_skew_seconds <= age <= config.max_recent_speech_age_seconds


def _event_refs(event: GroundedEvent, config: CognitionConfig) -> tuple[str, ...]:
    refs = [event.event_id, f"producer:{event.provenance.producer}"]
    if event.provenance.source_event_id:
        refs.append(event.provenance.source_event_id)
    return _safe_refs(tuple(refs), config)


def _safe_refs(
    value: Any,
    config: CognitionConfig,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        if allow_empty and value in (None, ()):
            return ()
        raise ValueError("references must be a sequence")
    result: list[str] = []
    for item in value:
        try:
            normalized = _bounded_id(item, "reference", config)
        except ValueError:
            continue
        if normalized not in result:
            result.append(normalized)
        if len(result) >= config.max_evidence_refs:
            break
    if not result and not allow_empty:
        raise ValueError("references must not be empty")
    return tuple(sorted(result))


def _safe_text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    clean = mask_pii(clean) or ""
    clean = clean.strip()
    if not clean:
        return None
    return clean[:max_chars].rstrip()


def _optional_safe_text(value: Any, max_chars: int) -> str | None:
    return None if value is None else _safe_text(value, max_chars)


def _bounded_id(value: Any, name: str, config: CognitionConfig) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{name} must be a trimmed string")
    if len(value) > config.max_id_chars:
        raise ValueError(f"{name} exceeds configured bound")
    return value


def _bounded_label(value: Any, name: str, config: CognitionConfig) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{name} must be a trimmed string")
    if len(value) > config.max_label_chars:
        raise ValueError(f"{name} exceeds configured bound")
    return value


def _derived_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _parse_time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc
    return _utc(parsed, name)


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _wire(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "datetime").isoformat()
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON cannot contain non-finite floats")
        return value
    raise ValueError("canonical JSON contains an unsupported value")


def _canonical_json(value: Any, *, exclude: set[str] | None = None) -> str:
    plain = _wire(value)
    if exclude and isinstance(plain, dict):
        plain = {key: item for key, item in plain.items() if key not in exclude}
    return json.dumps(
        plain,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _dedupe_contracts(values: list[Any], field_name: str) -> list[Any] | None:
    result: list[Any] = []
    seen: dict[str, str] = {}
    for value in values:
        identity = getattr(value, field_name)
        encoded = _canonical_json(value)
        if identity in seen:
            if seen[identity] != encoded:
                return None
            continue
        seen[identity] = encoded
        result.append(value)
    return result


def _call_metric(metrics: Any, method: str, *args: Any) -> None:
    recorder = getattr(metrics, method, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass
