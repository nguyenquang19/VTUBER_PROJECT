"""Test interfaces: model validation + stub behaviour (ARCHITECTURE 7.1-7.8).

Interface là contract — test chỗ có logic thật (HealthStatus helper, MoodState
dominant, FilterVerdict fail_open, NullSTTService stub) và verify các abstract
method đúng như spec để implementation phase sau không lệch.
"""
from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from interfaces.animation import (
    AnimationCommand,
    AnimationService,
    EmbodimentLevel,
    EmbodimentRecord,
    EmbodimentSnapshot,
    MoodState,
)
from interfaces.action_execution import LocalActionBoundaryService
from interfaces.agent import (
    AgentStateService, EventLedgerService, GoalManagerService, GoalProposalService,
    ConversationContextService, ConversationRepairService, OpenThreadManagerService,
    BehaviorLibraryService,
    SessionRecapService,
    ThreadExtractionService,
)
from interfaces.base import HealthState, HealthStatus, Service
from interfaces.filter import FilterCategory, FilterService, FilterVerdict
from interfaces.input import EventSource, InputEvent, InputService
from interfaces.human_like import HumanLikeCalibrationService
from interfaces.llm import LLMRequest, LLMService, LLMToken
from interfaces.memory import MemoryEntry, MemoryService, MemoryTier
from interfaces.operations import (
    DashboardDataSourceService,
    EmergencyControlService, HealthSupervisorService, IncidentLogService,
    OperationsSnapshotService, OperatorControlService, SoakMonitorService,
    ShutdownCoordinatorService,
)
from interfaces.stt import NullSTTService, STTService, TranscriptChunk
from interfaces.tts import AudioChunk, TTSRequest, TTSService
from interfaces.trajectory import TrajectoryRecordService


class TestHealthStatus:
    def test_healthy_helper(self) -> None:
        h = HealthStatus.healthy("llm_main", port=8080)
        assert h.state is HealthState.HEALTHY
        assert h.is_ok is True
        assert h.details["port"] == 8080

    def test_degraded_and_unhealthy_are_not_ok(self) -> None:
        assert HealthStatus.degraded("tts", "slow").is_ok is False
        assert HealthStatus.unhealthy("tts", "crashed").is_ok is False
        assert HealthStatus.stopped("tts").is_ok is False

    def test_message_carried(self) -> None:
        h = HealthStatus.unhealthy("llm_main", "connection refused", port=8080)
        assert h.message == "connection refused"
        assert h.details["port"] == 8080


class TestServiceContract:
    def test_service_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Service()  # type: ignore[abstract]

    @pytest.mark.parametrize(
        "iface,method",
        [
            (InputService, "event_stream"),
            (STTService, "transcribe_stream"),
            (LLMService, "generate_stream"),
            (LLMService, "cancel"),
            (FilterService, "check"),
            (TTSService, "synthesize_stream"),
            (TTSService, "cancel"),
            (AnimationService, "express"),
            (AnimationService, "trigger_intentional_gesture"),
            (AnimationService, "is_intentional_gesture_allowed"),
            (AnimationService, "sync_with_audio"),
            (LocalActionBoundaryService, "execute"),
            (LocalActionBoundaryService, "snapshot"),
            (MemoryService, "write"),
            (MemoryService, "query"),
            (MemoryService, "forget"),
            (EventLedgerService, "append"),
            (EventLedgerService, "recent"),
            (AgentStateService, "record"),
            (AgentStateService, "snapshot"),
            (AgentStateService, "set_active_goal_ref"),
            (AgentStateService, "add_event_listener"),
            (BehaviorLibraryService, "select"),
            (BehaviorLibraryService, "set_enabled"),
            (GoalManagerService, "submit"),
            (GoalManagerService, "complete"),
            (GoalManagerService, "cancel"),
            (GoalManagerService, "snapshot"),
            (GoalManagerService, "focus_delivered_thread"),
            (GoalManagerService, "clear_continue_threads"),
            (GoalManagerService, "pin_operator"),
            (GoalManagerService, "operator_complete"),
            (GoalManagerService, "operator_cancel"),
            (GoalManagerService, "handle_event"),
            (GoalManagerService, "accept_proposal"),
            (GoalProposalService, "propose"),
            (GoalProposalService, "set_enabled"),
            (OpenThreadManagerService, "create"),
            (OpenThreadManagerService, "update"),
            (OpenThreadManagerService, "resolve"),
            (OpenThreadManagerService, "expire"),
            (OpenThreadManagerService, "snapshot"),
            (ThreadExtractionService, "propose"),
            (ThreadExtractionService, "set_enabled"),
            (SessionRecapService, "handle_event"),
            (SessionRecapService, "snapshot"),
            (ConversationContextService, "render"),
            (ConversationRepairService, "decide"),
            (HealthSupervisorService, "register_target"),
            (HealthSupervisorService, "pause_recovery"),
            (HealthSupervisorService, "resume_recovery"),
            (HealthSupervisorService, "snapshot"),
            (ShutdownCoordinatorService, "register_step"),
            (ShutdownCoordinatorService, "shutdown"),
            (OperatorControlService, "pause"),
            (OperatorControlService, "resume"),
            (OperatorControlService, "record_operator_action"),
            (OperatorControlService, "snapshot"),
            (OperationsSnapshotService, "snapshot"),
            (DashboardDataSourceService, "snapshot_for"),
            (DashboardDataSourceService, "query_history"),
            (DashboardDataSourceService, "forward_command"),
            (EmergencyControlService, "trigger"),
            (EmergencyControlService, "resume"),
            (EmergencyControlService, "permits_speech"),
            (EmergencyControlService, "permits_environment_action"),
            (EmergencyControlService, "snapshot"),
            (SoakMonitorService, "run"),
            (SoakMonitorService, "snapshot"),
            (IncidentLogService, "record_incident"),
            (IncidentLogService, "resolve"),
            (IncidentLogService, "snapshot"),
            (HumanLikeCalibrationService, "build"),
            (HumanLikeCalibrationService, "finalize"),
            (HumanLikeCalibrationService, "snapshot"),
            (TrajectoryRecordService, "begin"),
            (TrajectoryRecordService, "mark_selection"),
            (TrajectoryRecordService, "record_action"),
            (TrajectoryRecordService, "record_result"),
            (TrajectoryRecordService, "record_no_action"),
            (TrajectoryRecordService, "replay"),
            (TrajectoryRecordService, "snapshot"),
        ],
    )
    def test_interface_declares_spec_method(self, iface, method) -> None:
        assert hasattr(iface, method), f"{iface.__name__} thiếu {method} (spec Section 7)"
        assert method in iface.__abstractmethods__

    @pytest.mark.parametrize(
        "iface",
        [
            InputService, STTService, LLMService, FilterService, TTSService,
            AnimationService, MemoryService, EventLedgerService, AgentStateService,
            GoalManagerService,
            GoalProposalService,
            OpenThreadManagerService,
            ThreadExtractionService,
            SessionRecapService,
            ConversationContextService,
            ConversationRepairService,
            HealthSupervisorService,
            ShutdownCoordinatorService,
            OperatorControlService,
            OperationsSnapshotService,
            DashboardDataSourceService,
            EmergencyControlService,
            SoakMonitorService,
            IncidentLogService,
            HumanLikeCalibrationService,
            TrajectoryRecordService,
        ],
    )
    def test_all_interfaces_inherit_service_base(self, iface) -> None:
        """N8: mọi service interface đều kế thừa Service base (start/stop/health/metrics)."""
        assert issubclass(iface, Service)
        for m in ("start", "stop", "health_check", "get_metrics"):
            assert m in iface.__abstractmethods__, f"{iface.__name__} thiếu abstract {m}"


class TestInputModels:
    def test_input_event_roundtrip(self) -> None:
        ev = InputEvent(
            event_id="e1",
            timestamp=datetime.now(timezone.utc),
            source=EventSource.CHAT_TWITCH,
            user_name="viewer_123",
            content="mai ơi hôm nay chơi gì",
        )
        assert ev.source is EventSource.CHAT_TWITCH
        assert ev.user_id is None
        assert ev.metadata == {}

    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InputEvent(
                event_id="e1",
                timestamp=datetime.now(timezone.utc),
                source="carrier_pigeon",  # type: ignore[arg-type]
                content="hi",
            )


class TestNullSTT:
    async def test_stub_yields_nothing(self) -> None:
        svc = NullSTTService()
        await svc.start()

        async def empty_audio():
            return
            yield b""  # pragma: no cover

        chunks = [c async for c in svc.transcribe_stream(empty_audio())]
        assert chunks == []
        await svc.stop()

    async def test_stub_reports_healthy(self) -> None:
        svc = NullSTTService()
        health = await svc.health_check()
        assert health.is_ok is True
        assert svc.get_metrics()["stt_chunks_total"] == 0

    def test_transcript_chunk_model(self) -> None:
        c = TranscriptChunk(
            chunk_id="c1", text="xin chào", is_final=True, audio_start_ms=0, audio_end_ms=1200
        )
        assert c.emotion is None
        assert c.audio_end_ms == 1200


class TestLLMModels:
    def test_defaults_match_spec(self) -> None:
        req = LLMRequest(request_id="r1", prompt="hi")
        assert req.max_tokens == 300
        assert req.temperature == 0.85
        assert req.stop_sequences == []

    def test_token_metadata_default_isolated(self) -> None:
        a = LLMToken(request_id="r1", token="x", is_final=False)
        b = LLMToken(request_id="r1", token="y", is_final=False)
        a.metadata["pos"] = 1
        assert b.metadata == {}


class TestFilterVerdict:
    def test_allow_helper(self) -> None:
        v = FilterVerdict.allow(latency_ms=12)
        assert v.passed is True
        assert v.categories_hit == []
        assert v.suggested_action == "allow"
        assert v.latency_ms == 12

    def test_fail_open_passes_with_reason(self) -> None:
        """N7: filter lỗi → cho qua, nhưng reason phải nói rõ là fail-open."""
        v = FilterVerdict.fail_open("regex compile error", latency_ms=3)
        assert v.passed is True
        assert v.suggested_action == "allow"
        assert "fail-open" in v.reason
        assert "regex compile error" in v.reason

    def test_blocking_verdict(self) -> None:
        v = FilterVerdict(
            passed=False,
            categories_hit=[FilterCategory.PERSONA_BREAK, FilterCategory.MANIPULATION],
            severity="high",
            suggested_action="regenerate",
            reason="persona break",
        )
        assert FilterCategory.PERSONA_BREAK in v.categories_hit
        assert len(v.categories_hit) == 2


class TestTTSModels:
    def test_request_defaults(self) -> None:
        r = TTSRequest(request_id="t1", text="xin chào")
        assert r.voice_id == "mai_default"
        assert r.emotion is None
        assert r.speed == 1.0

    def test_audio_chunk_holds_bytes(self) -> None:
        c = AudioChunk(
            request_id="t1", chunk_index=0, audio_bytes=b"\x00\x01", is_final=False, duration_ms=250
        )
        assert c.audio_bytes == b"\x00\x01"


class TestMoodState:
    def test_dominant_picks_highest(self) -> None:
        assert MoodState(vui=8, buon=2).dominant() == "vui"
        assert MoodState(buc=9, vui=3).dominant() == "buc"
        assert MoodState(nguong=5).dominant() == "nguong"

    def test_all_zero_is_neutral(self) -> None:
        assert MoodState().dominant() == "neutral"

    def test_range_enforced_0_to_10(self) -> None:
        with pytest.raises(ValidationError):
            MoodState(vui=11)
        with pytest.raises(ValidationError):
            MoodState(vui=-1)

    def test_five_moods_exactly(self) -> None:
        """N1 YAGNI: đúng 5 mood như spec 7.7, không thêm."""
        assert set(MoodState().model_dump()) == {"vui", "buon", "buc", "bon_chon", "nguong"}

    def test_animation_command_optional_mood(self) -> None:
        cmd = AnimationCommand(command_type="idle")
        assert cmd.mood is None
        assert cmd.intensity == 0.5

    def test_animation_contracts_are_strict_and_frozen(self) -> None:
        with pytest.raises(ValidationError):
            MoodState(vui="5")  # type: ignore[arg-type]
        mood = MoodState(vui=5)
        with pytest.raises(ValidationError):
            mood.vui = 6
        with pytest.raises(ValidationError):
            AnimationCommand(command_type=" idle ")

    def test_embodiment_snapshot_is_deeply_immutable(self) -> None:
        record = EmbodimentRecord(
            sequence=1,
            level=EmbodimentLevel.HIGH,
            outcome="high_verified",
            action_id="action-1",
            gesture_id="wave",
            evidence_refs=("event-1",),
            verification_source="vts_api_ack",
        )
        snapshot = EmbodimentSnapshot(
            running=True,
            enabled=True,
            active_level=None,
            active_action_id=None,
            active_gesture_id=None,
            counts={"high_verified": 1},
            recent=(record,),
        )
        with pytest.raises(TypeError):
            snapshot.counts["high_verified"] = 2  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            record.outcome = "changed"  # type: ignore[misc]
        assert snapshot.to_dict()["recent"][0]["verification_source"] == "vts_api_ack"


class TestMemoryModels:
    def test_entry_defaults(self) -> None:
        e = MemoryEntry(entry_id="m1", content="user thích mèo", timestamp=datetime.now(timezone.utc))
        assert e.tier is MemoryTier.WORKING
        assert e.importance == 0.5
        assert e.tags == ()

    def test_importance_bounded(self) -> None:
        with pytest.raises(ValueError):
            MemoryEntry(
                entry_id="m1", content="x", timestamp=datetime.now(timezone.utc), importance=1.5
            )

    def test_three_tiers(self) -> None:
        assert {t.value for t in MemoryTier} == {"working", "session", "persistent"}

    def test_entry_is_deeply_immutable_and_utc_strict(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            MemoryEntry(entry_id="m1", content="x", timestamp=datetime.now())
        with pytest.raises(ValueError, match="tuple"):
            MemoryEntry(
                entry_id="m1", content="x", timestamp=datetime.now(timezone.utc),
                tags=["legacy"],  # type: ignore[arg-type]
            )
        entry = MemoryEntry(
            entry_id="m1", content="x", timestamp=datetime.now(timezone.utc),
            metadata={"nested": {"values": ["a", "b"]}},
        )
        assert entry.metadata["nested"]["values"] == ("a", "b")
        with pytest.raises(TypeError):
            entry.metadata["new"] = "value"  # type: ignore[index]

    def test_success_memory_requires_authoritative_verification(self) -> None:
        with pytest.raises(ValueError, match="verified"):
            MemoryEntry(
                entry_id="m1", content="guest joined", timestamp=datetime.now(timezone.utc),
                metadata={"action_status": "succeeded", "provenance": "action_result"},
            )
        entry = MemoryEntry(
            entry_id="m2", content="guest joined", timestamp=datetime.now(timezone.utc),
            metadata={
                "action_status": "succeeded", "verified": True,
                "outcome_id": "outcome-1", "provenance": "action_result",
            },
        )
        assert entry.metadata["verified"] is True


class TestStreamingSignatures:
    """Method streaming phải là async generator hoặc trả AsyncIterator, không await-and-return."""

    @pytest.mark.parametrize(
        "iface,method",
        [
            (InputService, "event_stream"),
            (STTService, "transcribe_stream"),
            (LLMService, "generate_stream"),
            (TTSService, "synthesize_stream"),
        ],
    )
    def test_stream_methods_return_asynciterator(self, iface, method) -> None:
        sig = inspect.signature(getattr(iface, method))
        ann = str(sig.return_annotation)
        assert "AsyncIterator" in ann, f"{iface.__name__}.{method} phải trả AsyncIterator, có {ann}"
