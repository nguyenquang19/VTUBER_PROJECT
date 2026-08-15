"""Phase 1 V2 compatibility contracts remain isolated from the production runtime."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from interfaces.action_transaction import ActionTransaction, ActionTransactionState
from interfaces.compatibility import (
    ActionResult,
    ActionStatus,
    Capability,
    CapabilityAvailability,
    EventProvenance,
    PerceptionEvent,
    SelfSnapshot,
    StateValue,
    WorldSnapshot,
    action_request_from_transaction,
    action_result_from_tts_delivery,
    perception_event_from_input,
)
from interfaces.input import EventSource, InputEvent
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult


UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _perception(**overrides) -> PerceptionEvent:
    values = {
        "schema_version": 1,
        "event_id": "event-1",
        "source": "chat_youtube",
        "event_type": "input.received",
        "timestamp": NOW,
        "payload": {"content": "xin chào", "nested": {"index": 1}},
        "provenance": EventProvenance(producer="test", source_event_id="raw-1"),
    }
    values.update(overrides)
    return PerceptionEvent(**values)


class TestCompatibilityContractValues:
    def test_perception_normalizes_utc_and_deep_freezes(self) -> None:
        plus_seven = timezone(timedelta(hours=7))
        event = _perception(timestamp=NOW.astimezone(plus_seven), entities=("Mai",))

        assert event.timestamp.tzinfo is UTC
        assert event.timestamp == NOW
        assert event.entities == ("Mai",)
        with pytest.raises(TypeError):
            event.payload["content"] = "mutate"  # type: ignore[index]
        with pytest.raises(TypeError):
            event.payload["nested"]["index"] = 2  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            event.event_id = "other"  # type: ignore[misc]

    @pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan")])
    def test_invalid_confidence_is_rejected(self, confidence: float) -> None:
        with pytest.raises(ValueError, match="confidence"):
            _perception(confidence=confidence)

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _perception(timestamp=datetime(2026, 8, 15, 12, 0))

    def test_sensitive_payload_keys_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="sensitive key"):
            _perception(payload={"content": "hi", "viewer_id": "raw"})

    def test_contracts_serialize_to_json_safe_values(self) -> None:
        value = StateValue(
            value={"live": True}, source="operator", confidence=0.8,
            updated_at=NOW, evidence_refs=("event-1",),
        )
        world = WorldSnapshot(snapshot_id="world-1", created_at=NOW, stream={"live": value})
        self_snapshot = SelfSnapshot(
            snapshot_id="self-1", created_at=NOW, speaking=False, busy=False, degraded=False,
            current_action_id=None, current_intention_id=None, active_goal_id=None,
            focused_thread_id=None, current_topic=None, attention_target=None,
            avatar_state={"expression": "neutral"}, recent_action_ids=("action-1",),
        )
        capability = Capability(
            capability_id="speech.say", action_type="speech", description="Speak typed text",
            executor_id="tts", verifier_id="tts_delivery", risk_level="low",
            required_permissions=("speech",), parameter_schema={"text": {"type": "string"}},
            transaction_policy="delivery_required",
        )
        availability = CapabilityAvailability(
            capability_id="speech.say", available=True, reason_code="ready", checked_at=NOW,
            evidence_refs=("health:tts",),
        )

        rendered = [item.to_dict() for item in (value, world, self_snapshot, capability, availability)]
        assert all(json.loads(json.dumps(item))["snapshot_id"] in {"world-1", "self-1"}
                   for item in rendered[1:3])
        assert rendered[0]["updated_at"].endswith("+00:00")
        assert rendered[3]["parameter_schema"]["text"]["type"] == "string"
        assert rendered[4]["available"] is True

    def test_invalid_action_status_and_time_order_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="status"):
            ActionResult(
                schema_version=1, action_id="a1", status="invented",  # type: ignore[arg-type]
                started_at=NOW, completed_at=NOW, verified=False,
                verification_source=None, result_data={},
            )
        with pytest.raises(ValueError, match="cannot precede"):
            ActionResult(
                schema_version=1, action_id="a1", status=ActionStatus.SUCCESS,
                started_at=NOW, completed_at=NOW - timedelta(seconds=1), verified=False,
                verification_source=None, result_data={},
            )


class TestCompatibilityMappers:
    def test_input_mapper_sanitizes_identity_and_sensitive_metadata(self) -> None:
        event = InputEvent(
            event_id="input-1", timestamp=NOW, source=EventSource.CHAT_YOUTUBE,
            user_id="viewer-raw", user_name="viewer name", content="xin chào",
            metadata={
                "safe": "yes", "viewer_id": "metadata-identity", "access_token": "do-not-copy",
                "nested": {"password": "x", "kept": 2},
            },
        )

        mapped = perception_event_from_input(
            event, max_payload_items=8, max_payload_chars=256, session_id="session-1",
        )

        assert mapped.source == EventSource.CHAT_YOUTUBE.value
        assert mapped.payload == {"content": "xin chào", "metadata": {"safe": "yes", "nested": {"kept": 2}}}
        assert "viewer-raw" not in json.dumps(mapped.to_dict(), ensure_ascii=False)
        assert mapped.provenance.session_id == "session-1"
        assert mapped.dedup_key == "input-1"

    def test_input_mapper_requires_explicit_bounds(self) -> None:
        event = InputEvent(
            event_id="input-1", timestamp=NOW, source=EventSource.CHAT_YOUTUBE,
            content="payload too long",
        )
        with pytest.raises(ValueError, match="max_payload_chars"):
            perception_event_from_input(event, max_payload_items=8, max_payload_chars=4)
        with pytest.raises(ValueError, match="max_payload_items"):
            perception_event_from_input(event, max_payload_items=1, max_payload_chars=256)

    def test_legacy_provenance_adapter_has_no_service_import(self) -> None:
        legacy = SimpleNamespace(
            producer="agent_state", source_event_id="input-1", session_id="session-1",
            platform="youtube",
        )
        mapped = EventProvenance.from_legacy(legacy)
        assert mapped.to_dict() == {
            "producer": "agent_state", "source_event_id": "input-1",
            "session_id": "session-1", "platform": "youtube",
        }

    def test_transaction_and_tts_mappers_are_side_effect_free_values(self) -> None:
        transaction = ActionTransaction(
            transaction_id="tx-1", idempotency_key="same-turn", action="read_chat",
            state=ActionTransactionState.DELIVERED, created_at=1.0, updated_at=2.0,
        )
        request = action_request_from_transaction(
            transaction, capability_id="speech.say", requested_at=NOW,
            transaction_policy="delivery_required", arguments={"text": "xin chào"},
            evidence_refs=("input-1",), priority=12.5,
        )
        delivered = TTSDeliveryResult(
            request_id="tts-1", delivered=True, mode=TTSDeliveryMode.AUDIO,
            sentences_total=1, sentences_delivered=1, audio_sentences=1,
        )
        result = action_result_from_tts_delivery(
            delivered, action_id=request.action_id, started_at=NOW, completed_at=NOW,
        )

        assert request.action_type == "read_chat"
        assert request.idempotency_key == "same-turn"
        assert result.status is ActionStatus.SUCCESS
        assert result.verified is True
        assert result.verification_source == "tts_delivery"
        assert transaction.state is ActionTransactionState.DELIVERED


class TestRuntimeIsolation:
    def test_contract_mappers_are_not_wired_into_production_runtime(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for relative in (
            "orchestrator/stream_runtime.py",
            "services/input/chat_router.py",
            "services/director/director_loop.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            assert "interfaces.compatibility" not in source
            assert "perception_event_from_input" not in source
            assert "action_request_from_transaction" not in source
            assert "action_result_from_tts_delivery" not in source