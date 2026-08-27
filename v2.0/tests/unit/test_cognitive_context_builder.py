"""Deterministic read-only Cognitive Context Builder behavior."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from interfaces.cognition import (
    CognitionConfig,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
)
from interfaces.compatibility import (
    Capability,
    CapabilityAvailability,
    SelfSnapshot,
    StateValue,
    WorldSnapshot,
)
from interfaces.memory import MemoryEntry, MemoryTier
from orchestrator.config_loader import ConfigLoader
from services.memory.config import MemoryRuntimeConfig
from services.memory.recall_gate import RecallGate
from interfaces.state import DeliveredTurnRecord
from services.operations.metrics import MetricsCollector
from interfaces.state import GoalSnapshot
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
    OpenThread,
    ThreadContribution,
    ThreadEvidence,
    ThreadKind,
    ThreadSpeaker,
    ThreadStatus,
)
from services.cognition.context_builder import CognitiveContextBuilder


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def _config(**updates: object) -> CognitionConfig:
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    raw.update(updates)
    return CognitionConfig.from_mapping(raw)


def _hard(config: CognitionConfig, **updates: bool) -> CognitiveHardState:
    values = {
        "emergency": False,
        "operator_hold": False,
        "safety_hold": False,
        "permission_hold": False,
        "transaction_conflict": False,
        "critical_state": False,
    }
    values.update(updates)
    return CognitiveHardState(
        config=config,
        schema_version=1,
        source_failure_codes=(),
        **values,
    )


def _request(
    config: CognitionConfig,
    *,
    requested_at: datetime = NOW,
    trigger: str | None = "chat-1",
    hard: CognitiveHardState | None = None,
) -> CognitiveContextRequest:
    return CognitiveContextRequest(
        config=config,
        schema_version=1,
        request_id=f"request-{int(requested_at.timestamp())}",
        session_id="session-1",
        requested_at=requested_at,
        trigger_event_ref=trigger,
        hard_state=hard or _hard(config),
    )


def _event(
    event_id: str,
    kind: AgentEventKind,
    payload: dict[str, object],
    *,
    timestamp: datetime,
    source: AgentEventSource = AgentEventSource.DIRECTOR,
) -> GroundedEvent:
    return GroundedEvent(
        event_id=event_id,
        kind=kind,
        source=source,
        timestamp=timestamp,
        confidence=1.0,
        payload=payload,
        provenance=EventProvenance(
            producer="test", source_event_id=f"source:{event_id}", session_id="session-1",
        ),
    )


def _sources(
    *,
    now: datetime = NOW,
    focused_thread_id: str | None = "thread-1",
    memory_entries: list[MemoryEntry] | None = None,
) -> dict[str, object]:
    chat = _event(
        "chat-1",
        AgentEventKind.CHAT_RECEIVED,
        {"text": "Mai hãy nhớ email test@example.com rồi nói về cà phê"},
        timestamp=now - timedelta(seconds=5),
        source=AgentEventSource.YOUTUBE,
    )
    speech_final = _event(
        "speech-final-1",
        AgentEventKind.SPEECH_FINAL,
        {"text": "Câu mới chỉ được tạo"},
        timestamp=now - timedelta(seconds=3),
    )
    speech_completed = _event(
        "speech-completed-1",
        AgentEventKind.SPEECH_COMPLETED,
        {"text": "Tớ thích cà phê rang đậm.", "action": "read_chat"},
        timestamp=now - timedelta(seconds=2),
    )
    evidence = ThreadEvidence("chat-1", "nói về cà phê", "rule", 1.0)
    thread = OpenThread(
        thread_id="thread-1",
        topic="Cà phê",
        summary="Đang nói về cà phê rang đậm",
        created_at=now - timedelta(seconds=20),
        updated_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=10),
        kind=ThreadKind.QUESTION,
        evidence=(evidence,),
        origin_event_id="chat-1",
        status=ThreadStatus.ACTIVE,
        claims=(
            ThreadContribution("speech-final-1", "Câu mới chỉ được tạo", ThreadSpeaker.MAI),
            ThreadContribution(
                "speech-completed-1", "Tớ thích cà phê rang đậm.", ThreadSpeaker.MAI,
            ),
        ),
        open_questions=(
            ThreadContribution("chat-1", "Mai thích loại cà phê nào?", ThreadSpeaker.VIEWER),
        ),
        move_count=2,
    )
    agent = AgentStateSnapshot(
        open_threads=(thread,),
        recent_events=(chat, speech_final, speech_completed),
    )
    world = WorldSnapshot(
        snapshot_id="world-1",
        created_at=now,
        stream={
            "topic": StateValue(
                value="coffee",
                source="runtime",
                confidence=1.0,
                updated_at=now - timedelta(seconds=4),
                evidence_refs=("world-source-1",),
                expires_at=now + timedelta(minutes=1),
                authority=60,
            ),
        },
    )
    self_state = SelfSnapshot(
        snapshot_id="self-1",
        created_at=now,
        speaking=False,
        busy=False,
        degraded=False,
        current_action_id=None,
        current_intention_id=None,
        active_goal_id=None,
        focused_thread_id=focused_thread_id,
        current_topic="Cà phê",
        attention_target="chat-1",
        avatar_state={},
        recent_action_ids=(),
    )
    capability = Capability(
        capability_id="WAIT",
        action_type="WAIT",
        description="Wait safely",
        executor_id="secret-executor",
        verifier_id="secret-verifier",
        risk_level="low",
        required_permissions=("conversation.read",),
        parameter_schema={},
        transaction_policy="none",
    )
    availability = CapabilityAvailability(
        capability_id="WAIT",
        available=True,
        reason_code="available",
        checked_at=now,
        evidence_refs=("permission:conversation.read", "health:local"),
    )
    registry = {
        "enabled": True,
        "capabilities": [{
            "capability": capability.to_dict(),
            "availability": availability.to_dict(),
            "mock_only": False,
        }],
    }
    entries = memory_entries if memory_entries is not None else [_session_memory(now)]
    return {
        "world": world,
        "self": self_state,
        "agent": agent,
        "thread": (thread,),
        "goal": GoalSnapshot(),
        "capability": registry,
        "memory": entries,
    }


def _session_memory(
    now: datetime, *, world_path: str | None = None, importance: float = 0.5,
) -> MemoryEntry:
    metadata: dict[str, object] = {
        "cognitive_kind": "EPISODIC",
        "cognitive_scope": "SESSION",
        "provenance_refs": ["memory-source-1"],
        "confidence": 0.8,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    if world_path is not None:
        metadata["world_path"] = world_path
    return MemoryEntry(
        entry_id="memory-session",
        content="Mai từng nói về cà phê.",
        timestamp=now - timedelta(minutes=1),
        tier=MemoryTier.SESSION,
        importance=importance,
        metadata=metadata,
    )


def _viewer_memory(now: datetime) -> MemoryEntry:
    return MemoryEntry(
        entry_id="memory-viewer",
        content="Một người xem thích cà phê sữa.",
        timestamp=now - timedelta(minutes=1),
        tier=MemoryTier.PERSISTENT,
        metadata={
            "cognitive_kind": "PREFERENCE",
            "cognitive_scope": "VIEWER",
            "viewer_id": "v_0123456789abcdef",
            "provenance_refs": ["memory-source-viewer"],
            "confidence": 0.9,
        },
    )


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def snapshot(self) -> object:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class _Memory:
    def __init__(self, entries: list[MemoryEntry] | BaseException) -> None:
        self.entries = entries
        self.calls: list[tuple[str, int, object]] = []

    async def query(
        self, query_text: str, top_k: int = 3, tier=None, viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        self.calls.append((query_text, top_k, viewer_id))
        if isinstance(self.entries, BaseException):
            raise self.entries
        return self.entries


def _builder(
    config: CognitionConfig,
    sources: dict[str, object],
    *,
    clock: datetime = NOW,
    metrics: MetricsCollector | None = None,
    memory: _Memory | None = None,
    continuity: object | None = None,
    recall_gate: RecallGate | None = None,
) -> CognitiveContextBuilder:
    return CognitiveContextBuilder(
        config,
        world_model=_Snapshot(sources["world"]),
        self_model=_Snapshot(sources["self"]),
        capability_registry=_Snapshot(sources["capability"]),
        agent_state=_Snapshot(sources["agent"]),
        goal_manager=_Snapshot(sources["goal"]),
        thread_manager=_Snapshot(sources["thread"]),
        memory_service=memory or _Memory(sources["memory"]),  # type: ignore[arg-type]
        continuity_service=continuity,
        recall_gate=recall_gate,
        metrics=metrics,
        clock=lambda: clock,
    )


def _recall_gate(*, enabled: bool = True) -> RecallGate:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return RecallGate(MemoryRuntimeConfig.from_loader(loader), enabled=enabled)


@pytest.mark.asyncio
async def test_same_input_builds_same_context_without_duplicate_retention() -> None:
    config = _config()
    builder = _builder(config, _sources())
    await builder.start()
    first = await builder.build(_request(config))
    second = await builder.build(_request(config))
    assert first is not None and second is not None
    assert first.context_id == second.context_id
    assert first.context_id.startswith("ctx:")
    assert len(builder.recent()) == 1
    assert first.available_modes == (CognitiveMode.WAIT, CognitiveMode.SPEAK)
    assert first.available_actions == ()
    assert first.chat_digest is not None
    assert "test@example.com" not in first.chat_digest.summary
    assert "[PII]" in first.chat_digest.summary


@pytest.mark.asyncio
async def test_donation_trigger_is_projected_as_bounded_chat_evidence() -> None:
    config = _config()
    sources = _sources()
    donation = _event(
        "donation-1", AgentEventKind.DONATION_RECEIVED,
        {"text": "Tặng Mai một ly cà phê"}, timestamp=NOW,
        source=AgentEventSource.YOUTUBE,
    )
    agent = sources["agent"]
    assert isinstance(agent, AgentStateSnapshot)
    sources["agent"] = AgentStateSnapshot(
        open_threads=agent.open_threads,
        recent_events=(*agent.recent_events, donation),
    )
    builder = _builder(config, sources)
    await builder.start()
    context = await builder.build(_request(config, trigger="donation-1"))
    assert context is not None and context.chat_digest is not None
    assert context.chat_digest.evidence_id == "donation-1"
    assert context.chat_digest.summary == "Tặng Mai một ly cà phê"


@pytest.mark.asyncio
async def test_hard_hold_collapses_modes_to_wait() -> None:
    config = _config()
    builder = _builder(config, _sources())
    await builder.start()
    context = await builder.build(_request(
        config, hard=_hard(config, operator_hold=True),
    ))
    assert context is not None
    assert context.available_modes == (CognitiveMode.WAIT,)


@pytest.mark.asyncio
async def test_required_source_failure_returns_none_without_cached_fallback() -> None:
    config = _config()
    sources = _sources()
    sources["world"] = RuntimeError("world failed")
    builder = _builder(config, sources)
    await builder.start()
    assert await builder.build(_request(config)) is None
    assert builder.recent() == ()
    assert builder.get_metrics()["cognitive_context_builder_builds"] == {"unavailable": 1}


@pytest.mark.asyncio
async def test_stale_request_is_rejected_before_reading_sources() -> None:
    config = _config()
    builder = _builder(config, _sources(), clock=NOW)
    await builder.start()
    stale = NOW - timedelta(seconds=config.max_context_request_age_seconds + 1)
    assert await builder.build(_request(config, requested_at=stale)) is None
    assert builder.get_metrics()["cognitive_context_builder_builds"] == {"rejected": 1}


@pytest.mark.asyncio
async def test_viewer_memory_is_omitted_and_world_truth_wins_conflict() -> None:
    config = _config()
    sources = _sources(memory_entries=[
        _viewer_memory(NOW),
        _session_memory(NOW, world_path="stream.topic"),
    ])
    memory = _Memory(sources["memory"])  # type: ignore[arg-type]
    builder = _builder(config, sources, memory=memory)
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert context.memory_items == ()
    assert memory.calls[0][1:] == (config.memory_query_top_k, None)


@pytest.mark.asyncio
async def test_optional_memory_failure_yields_degraded_context() -> None:
    config = _config()
    builder = _builder(
        config, _sources(), memory=_Memory(RuntimeError("memory failed")),
    )
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert "memory" in context.operator_state.source_failure_codes
    assert builder.get_metrics()["cognitive_context_builder_builds"] == {"degraded": 1}


@pytest.mark.asyncio
async def test_brain_memory_projection_uses_hint_and_never_raw_content() -> None:
    raw = "Một câu memory nguyên văn không được đi vào Brain prompt."
    original = _session_memory(NOW, importance=0.9)
    entry = MemoryEntry(
        entry_id=original.entry_id,
        content=raw,
        timestamp=original.timestamp,
        tier=original.tier,
        importance=original.importance,
        metadata=original.metadata,
    )
    sources = _sources(memory_entries=[entry])
    gate = _recall_gate()
    config = _config()
    builder = _builder(config, sources, recall_gate=gate)
    await builder.start()
    first = await builder.build(_request(config))
    second = await builder.build(_request(config))
    assert first is not None and len(first.memory_items) == 1
    assert "subtle continuity cue" in first.memory_items[0].summary
    assert raw not in first.memory_items[0].summary
    assert second is not None and second.memory_items == ()


@pytest.mark.asyncio
async def test_bounded_cache_evicts_and_stop_clears_all_snapshots() -> None:
    config = _config(max_context_snapshots=2)
    metrics = MetricsCollector()
    builder = _builder(config, _sources(), clock=NOW + timedelta(seconds=2), metrics=metrics)
    await builder.start()
    for offset in range(3):
        assert await builder.build(_request(
            config,
            requested_at=NOW + timedelta(seconds=offset),
            trigger=None,
        )) is not None
    assert len(builder.recent()) == 2
    assert metrics.cognition_context_snapshot()["evicted"] == {"context": 1}
    await builder.stop()
    assert builder.recent() == ()
    assert builder.focus_snapshot() is None


class _CompatibilityProjection:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def get_metrics(self) -> dict[str, int]:
        return {"conversation_context_renders_total": 7}


@pytest.mark.asyncio
async def test_builder_owns_exact_compatibility_projection_lifecycle() -> None:
    config = _config()
    sources = _sources()
    projection = _CompatibilityProjection()
    agent_view = object()
    builder = CognitiveContextBuilder(
        config,
        world_model=_Snapshot(sources["world"]),
        self_model=_Snapshot(sources["self"]),
        capability_registry=_Snapshot(sources["capability"]),
        agent_state=_Snapshot(sources["agent"]),
        agent_context_projection=agent_view,
        conversation_context_projection=projection,
    )
    assert builder.agent_context_view is agent_view
    assert builder.conversation_context_view is projection
    await builder.start()
    assert projection.started == 1
    assert builder.get_metrics()["conversation_context_renders_total"] == 7
    await builder.stop()
    assert projection.stopped == 1


@pytest.mark.asyncio
async def test_recent_speech_reads_verified_continuity_records_when_composed() -> None:
    config = _config()
    record = DeliveredTurnRecord(
        schema_version=1,
        continuity_id="continuity:verified",
        outcome_ref="outcome:verified",
        transaction_id="transaction:verified",
        delivery_id="delivery-verified",
        session_id="session-1",
        source_mode="chat",
        action_type="read_chat",
        speech_text="Đây là câu đã tới người xem.",
        history_input="Cậu nói gì?",
        ref_event_ids=("chat-1",),
        goal_id=None,
        intention_id=None,
        thread_id="thread-1",
        conversation_move="deepen",
        viewer_ref=None,
        trigger_type="youtube",
        output_ok=True,
        mood_dominant="neutral",
        mood_intensity=0,
        delivered_at=NOW - timedelta(seconds=1),
        evidence_refs=("outcome:verified",),
    )

    class Continuity:
        def recent(self, _limit=None):
            return (record,)

    builder = _builder(config, _sources(), continuity=Continuity())
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert len(context.recent_delivered_speech) == 1
    assert context.recent_delivered_speech[0].delivery_id == "delivery-verified"
    assert context.recent_delivered_speech[0].speech_text == record.speech_text

def test_config_rejects_invalid_context_and_focus_bounds() -> None:
    with pytest.raises(ValueError, match="memory_query_top_k"):
        _config(memory_query_top_k=17)
    with pytest.raises(ValueError, match="exact thread statuses"):
        _config(focus_pressure_by_status={"active": 1.0})
    with pytest.raises(ValueError, match="within"):
        _config(focus_pressure_by_status={
            "active": 1.1, "waiting": 0.75, "parked": 0.25,
        })


def test_context_metrics_reject_unbounded_labels() -> None:
    metrics = MetricsCollector()
    metrics.record_cognitive_context_build("ready")
    metrics.record_cognitive_context_source("world", "accepted")
    metrics.record_cognitive_focus_projection("present")
    metrics.record_cognitive_snapshot_evicted("context")
    with pytest.raises(ValueError, match="unsupported"):
        metrics.record_cognitive_context_source("viewer-id", "accepted")
    snapshot = metrics.cognition_context_snapshot()
    assert snapshot == {
        "build": {"ready": 1},
        "source": {"world:accepted": 1},
        "focus": {"present": 1},
        "evicted": {"context": 1},
    }


def test_builder_module_has_no_live_runtime_or_llm_dependency() -> None:
    source = (
        ROOT / "services" / "cognition" / "context_builder.py"
    ).read_text(encoding="utf-8")
    assert "stream_runtime" not in source.casefold()
    assert "director_loop" not in source.casefold()
    assert "llama" not in source.casefold()
    assert "CognitiveBrainService" not in source
