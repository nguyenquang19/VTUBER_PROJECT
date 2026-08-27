"""MCB Cognitive Brain contracts remain strict, immutable, and shadow-only."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from interfaces.cognition import (
    CognitionConfig,
    CognitiveActionEnvelope,
    CognitiveActionProposal,
    CognitiveContext,
    CognitiveConversationState,
    CognitiveEvidenceItem,
    CognitiveEvidenceSource,
    CognitiveGroundingDecision,
    CognitiveGroundingOutcome,
    CognitiveHardState,
    CognitiveMemoryItem,
    CognitiveMode,
    CognitiveSpeechSummary,
    CognitiveTurn,
    CognitiveUncertainty,
    FocusOperation,
    FocusOrigin,
    FocusProposal,
    FocusState,
    MemoryClaimBasis,
    MemoryKind,
    MemoryProposal,
    MemoryRetentionClass,
    MemoryScope,
)
from orchestrator.config_loader import ConfigLoader
from orchestrator.config_loader import ConfigError
from orchestrator.features import FeatureManager, FeatureStatus
from services.operations.metrics import MetricsCollector


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
CONTEXT_ID = "a" * 64


@pytest.fixture(scope="module")
def cognition_config() -> CognitionConfig:
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    return CognitionConfig.from_mapping(raw)


def _evidence(config: CognitionConfig) -> CognitiveEvidenceItem:
    return CognitiveEvidenceItem(
        config=config,
        schema_version=1,
        evidence_id="event-1",
        source=CognitiveEvidenceSource.CHAT,
        summary="Viewer asked for a scene change.",
        provenance_refs=("source-1",),
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )


def _action(config: CognitionConfig) -> CognitiveActionEnvelope:
    return CognitiveActionEnvelope(
        config=config,
        schema_version=1,
        capability_id="switch-scene",
        action_type="SWITCH_SCENE",
        description="Request a verified scene change.",
        argument_schema={"scene_name": "string"},
        target_required=True,
        allows_speech=False,
        availability_ref="availability-1",
        checked_at=NOW,
        evidence_refs=("event-1",),
    )


def _context(
    config: CognitionConfig, *, focus_snapshot_id: str | None = None,
) -> CognitiveContext:
    evidence = _evidence(config)
    return CognitiveContext(
        config=config,
        schema_version=1,
        context_id=CONTEXT_ID,
        created_at=NOW,
        session_id="session-1",
        world_snapshot_id="world-1",
        self_snapshot_id="self-1",
        capability_snapshot_id="capabilities-1",
        focus_snapshot_id=focus_snapshot_id,
        operator_state=CognitiveHardState(
            config=config,
            schema_version=1,
            emergency=False,
            operator_hold=False,
            safety_hold=False,
            permission_hold=False,
            transaction_conflict=False,
            critical_state=False,
            source_failure_codes=(),
        ),
        available_modes=(
            CognitiveMode.WAIT, CognitiveMode.SPEAK, CognitiveMode.PROPOSE_ACTION,
        ),
        available_actions=(_action(config),),
        chat_digest=evidence,
        attention_items=(),
        conversation_state=CognitiveConversationState(
            config=config,
            schema_version=1,
            topic="Scene selection",
            thread_ref=None,
            goal_ref=None,
            intention_ref="intention-1",
            summary=None,
            evidence_refs=("event-1",),
        ),
        memory_items=(CognitiveMemoryItem(
            config=config,
            schema_version=1,
            memory_ref="memory-1",
            kind=MemoryKind.PREFERENCE,
            summary="The viewer prefers calm scenes.",
            scope=MemoryScope.VIEWER,
            viewer_ref="viewer-pseudo-1",
            provenance_refs=("source-1",),
            observed_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=5),
            confidence=0.8,
        ),),
        recent_delivered_speech=(CognitiveSpeechSummary(
            config=config,
            schema_version=1,
            delivery_id="delivery-1",
            speech_text="Để tớ xem nào.",
            delivered_at=NOW - timedelta(seconds=5),
            source_mode="read_chat",
            evidence_refs=("event-1",),
        ),),
    )


def _focus_create(config: CognitionConfig) -> FocusProposal:
    return FocusProposal(
        config=config,
        schema_version=1,
        proposal_id="focus-proposal-1",
        context_id=CONTEXT_ID,
        operation=FocusOperation.CREATE,
        base_focus_id=None,
        topic="Choose a scene",
        stance=None,
        unresolved_items=("Which scene?",),
        continuation_pressure=0.7,
        saturation=0.1,
        origin=FocusOrigin.CHAT,
        evidence_refs=("event-1",),
    )


def _action_proposal(config: CognitionConfig) -> CognitiveActionProposal:
    return CognitiveActionProposal(
        config=config,
        schema_version=1,
        proposal_id="action-proposal-1",
        context_id=CONTEXT_ID,
        capability_id="switch-scene",
        action_type="SWITCH_SCENE",
        target_ref="scene-main",
        arguments={"scene_name": "Main"},
        intention_id="intention-1",
        evidence_refs=("event-1",),
    )


def test_canonical_config_loads_strictly(cognition_config: CognitionConfig) -> None:
    assert cognition_config.max_brain_inflight == 1
    assert cognition_config.focus_ttl_seconds == 900
    assert cognition_config.max_speech_chars == 512
    assert (
        cognition_config.grounding_uncertainty_threshold
        is CognitiveUncertainty.MEDIUM
    )
    assert cognition_config.grounding_evidence_policy == "all_current"


def test_invalid_cognition_reload_keeps_whole_prior_config(tmp_path: Path) -> None:
    path = tmp_path / "cognition.yaml"
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    loader = ConfigLoader(tmp_path, required=("cognition",))
    loader.load_all()
    before = loader.section("cognition")
    raw["max_id_chars"] = True
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert loader.reload_file("cognition") is False
    assert loader.section("cognition") == before


def test_initial_invalid_cognition_config_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "cognition.yaml"
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    raw["unknown"] = 1
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    loader = ConfigLoader(tmp_path, required=("cognition",))
    with pytest.raises(ConfigError, match="unknown"):
        loader.load_all()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw.update(extra=True),
        lambda raw: raw.pop("max_id_chars"),
        lambda raw: raw.update(schema_version=True),
        lambda raw: raw.update(reason_codes=["same", "same"]),
        lambda raw: raw.update(max_speech_chars=4096),
        lambda raw: raw.update(max_brain_speech_chars=513),
        lambda raw: raw.update(grounding_uncertainty_threshold="HIGH"),
        lambda raw: raw.update(grounding_evidence_policy="any_current"),
    ],
)
def test_cognition_config_rejects_unknown_missing_coercion_and_invalid_bounds(mutator) -> None:
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    mutator(raw)
    with pytest.raises(ValueError):
        CognitionConfig.from_mapping(raw)


def test_contracts_are_deeply_immutable(cognition_config: CognitionConfig) -> None:
    context = _context(cognition_config)
    with pytest.raises(FrozenInstanceError):
        context.session_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.available_actions[0].argument_schema["scene_name"] = "integer"  # type: ignore[index]


def test_init_only_validation_inputs_are_not_wire_fields(cognition_config: CognitionConfig) -> None:
    assert "config" not in {item.name for item in fields(CognitiveContext)}
    assert "context" not in {item.name for item in fields(CognitiveTurn)}


def test_context_rejects_expired_items(cognition_config: CognitionConfig) -> None:
    kwargs = _context_kwargs(cognition_config)
    kwargs["chat_digest"] = CognitiveEvidenceItem(
        config=cognition_config, schema_version=1, evidence_id="expired",
        source=CognitiveEvidenceSource.CHAT, summary="old",
        provenance_refs=("source-old",), observed_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="omit expired"):
        CognitiveContext(**kwargs)


def test_strict_scalar_time_and_capacity_rejection(cognition_config: CognitionConfig) -> None:
    with pytest.raises(ValueError, match="bool"):
        CognitiveHardState(
            config=cognition_config, schema_version=1,
            emergency=1, operator_hold=False, safety_hold=False,
            permission_hold=False, transaction_conflict=False,
            critical_state=False, source_failure_codes=(),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        CognitiveEvidenceItem(
            config=cognition_config, schema_version=1, evidence_id="event-1",
            source=CognitiveEvidenceSource.CHAT, summary="summary",
            provenance_refs=("source-1",), observed_at=datetime(2026, 8, 23),
            expires_at=None,
        )
    with pytest.raises(ValueError, match="capacity"):
        CognitiveContext(
            **{
                **_context_kwargs(cognition_config),
                "attention_items": tuple(
                    CognitiveEvidenceItem(
                        config=cognition_config, schema_version=1,
                        evidence_id=f"event-{index}",
                        source=CognitiveEvidenceSource.CHAT, summary="summary",
                        provenance_refs=(f"source-{index}",), observed_at=NOW,
                        expires_at=None,
                    )
                    for index in range(cognition_config.max_attention_items + 1)
                ),
            }
        )


def _context_kwargs(config: CognitionConfig) -> dict[str, object]:
    context = _context(config)
    return {
        "config": config,
        "schema_version": context.schema_version,
        "context_id": context.context_id,
        "created_at": context.created_at,
        "session_id": context.session_id,
        "world_snapshot_id": context.world_snapshot_id,
        "self_snapshot_id": context.self_snapshot_id,
        "capability_snapshot_id": context.capability_snapshot_id,
        "focus_snapshot_id": context.focus_snapshot_id,
        "operator_state": context.operator_state,
        "available_modes": context.available_modes,
        "available_actions": context.available_actions,
        "chat_digest": context.chat_digest,
        "attention_items": context.attention_items,
        "conversation_state": context.conversation_state,
        "memory_items": context.memory_items,
        "recent_delivered_speech": context.recent_delivered_speech,
    }


def test_wait_is_exact_and_has_no_second_free_text_reason(cognition_config: CognitionConfig) -> None:
    context = _context(cognition_config)
    turn = CognitiveTurn(
        config=cognition_config, context=context, schema_version=1,
        turn_id="turn-1", context_id=CONTEXT_ID, mode=CognitiveMode.WAIT,
        attention_target_id="event-1", intent=None, speech_text=None,
        action_proposal=None, focus_proposal=None, memory_proposals=(),
        evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.UNKNOWN,
        reason_codes=("intentional_wait",),
    )
    assert turn.mode is CognitiveMode.WAIT
    with pytest.raises(ValueError, match="WAIT"):
        CognitiveTurn(
            config=cognition_config, context=context, schema_version=1,
            turn_id="turn-2", context_id=CONTEXT_ID, mode=CognitiveMode.WAIT,
            attention_target_id=None, intent="wait because quiet", speech_text=None,
            action_proposal=None, focus_proposal=None, memory_proposals=(),
            evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.LOW,
            reason_codes=("intentional_wait",),
        )


def test_grounding_decision_requires_pass_or_effective_wait(
    cognition_config: CognitionConfig,
) -> None:
    context = _context(cognition_config)
    speech = CognitiveTurn(
        config=cognition_config, context=context, schema_version=1,
        turn_id="turn-grounded", context_id=CONTEXT_ID,
        mode=CognitiveMode.SPEAK, attention_target_id="event-1",
        intent="Acknowledge the viewer", speech_text="Tớ nghe thấy rồi.",
        action_proposal=None, focus_proposal=None, memory_proposals=(),
        evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.LOW,
        reason_codes=("propose_speech",),
    )
    with pytest.raises(ValueError, match="effective WAIT"):
        CognitiveGroundingDecision(
            config=cognition_config, schema_version=1,
            source_turn_id=speech.turn_id, context_id=context.context_id,
            source_mode=speech.mode, source_uncertainty=speech.uncertainty,
            outcome=CognitiveGroundingOutcome.SUPPRESSED_EMPTY_EVIDENCE,
            effective_turn=speech,
        )
    with pytest.raises(ValueError, match="non-WAIT"):
        CognitiveGroundingDecision(
            config=cognition_config, schema_version=1,
            source_turn_id="source-wait", context_id=context.context_id,
            source_mode=CognitiveMode.WAIT,
            source_uncertainty=speech.uncertainty,
            outcome=CognitiveGroundingOutcome.PASSED,
            effective_turn=speech,
        )


def test_speak_and_action_mode_matrix(cognition_config: CognitionConfig) -> None:
    context = _context(cognition_config)
    speech = CognitiveTurn(
        config=cognition_config, context=context, schema_version=1,
        turn_id="turn-speak", context_id=CONTEXT_ID, mode=CognitiveMode.SPEAK,
        attention_target_id="event-1", intent="Acknowledge the viewer",
        speech_text="Đổi cảnh à, để tớ xem.", action_proposal=None,
        focus_proposal=_focus_create(cognition_config), memory_proposals=(),
        evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.LOW,
        reason_codes=("propose_speech",),
    )
    assert speech.speech_text is not None
    action = CognitiveTurn(
        config=cognition_config, context=context, schema_version=1,
        turn_id="turn-action", context_id=CONTEXT_ID,
        mode=CognitiveMode.PROPOSE_ACTION, attention_target_id="event-1",
        intent="Propose a verified scene change", speech_text=None,
        action_proposal=_action_proposal(cognition_config), focus_proposal=None,
        memory_proposals=(), evidence_refs=("event-1",),
        uncertainty=CognitiveUncertainty.MEDIUM,
        reason_codes=("propose_action",),
    )
    assert action.action_proposal is not None
    with pytest.raises(ValueError, match="does not match"):
        bad = _action_proposal_kwargs(cognition_config)
        bad["arguments"] = {"scene_name": 1}
        proposal = CognitiveActionProposal(**bad)
        CognitiveTurn(
            config=cognition_config, context=context, schema_version=1,
            turn_id="turn-bad", context_id=CONTEXT_ID,
            mode=CognitiveMode.PROPOSE_ACTION, attention_target_id="event-1",
            intent="bad", speech_text=None, action_proposal=proposal,
            focus_proposal=None, memory_proposals=(), evidence_refs=("event-1",),
            uncertainty=CognitiveUncertainty.HIGH,
            reason_codes=("propose_action",),
        )


def _action_proposal_kwargs(config: CognitionConfig) -> dict[str, object]:
    return {
        "config": config, "schema_version": 1,
        "proposal_id": "action-proposal-2", "context_id": CONTEXT_ID,
        "capability_id": "switch-scene", "action_type": "SWITCH_SCENE",
        "target_ref": "scene-main", "arguments": {"scene_name": "Main"},
        "intention_id": "intention-1", "evidence_refs": ("event-1",),
    }


def test_focus_operation_matrix_and_stale_base(cognition_config: CognitionConfig) -> None:
    context = _context(cognition_config)
    create = _focus_create(cognition_config)
    CognitiveTurn(
        config=cognition_config, context=context, schema_version=1,
        turn_id="turn-focus", context_id=CONTEXT_ID, mode=CognitiveMode.SPEAK,
        attention_target_id="event-1", intent="Continue topic", speech_text="Ừ, tiếp nhé.",
        action_proposal=None, focus_proposal=create, memory_proposals=(),
        evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.LOW,
        reason_codes=("focus_continuity",),
    )
    with pytest.raises(ValueError, match="CREATE is invalid"):
        CognitiveTurn(
            config=cognition_config,
            context=_context(cognition_config, focus_snapshot_id="focus-active"),
            schema_version=1, turn_id="turn-stale", context_id=CONTEXT_ID,
            mode=CognitiveMode.SPEAK, attention_target_id="event-1",
            intent="Continue", speech_text="Tiếp nhé.", action_proposal=None,
            focus_proposal=create, memory_proposals=(), evidence_refs=("event-1",),
            uncertainty=CognitiveUncertainty.LOW,
            reason_codes=("focus_continuity",),
        )
    with pytest.raises(ValueError, match="KEEP"):
        FocusProposal(
            config=cognition_config, schema_version=1, proposal_id="focus-keep",
            context_id=CONTEXT_ID, operation=FocusOperation.KEEP,
            base_focus_id="focus-active", topic="mutation", stance=None,
            unresolved_items=(), continuation_pressure=None, saturation=None,
            origin=None, evidence_refs=(),
        )


def test_focus_state_ttl_is_config_owned(cognition_config: CognitionConfig) -> None:
    with pytest.raises(ValueError, match="focus_ttl_seconds"):
        FocusState(
            config=cognition_config, schema_version=1, focus_id="focus-1",
            topic="topic", stance=None, unresolved_items=(), claims_delivered=(),
            continuation_pressure=0.2, saturation=0.2, origin=FocusOrigin.CHAT,
            evidence_refs=("event-1",), born_at=NOW, updated_at=NOW,
            expires_at=NOW + timedelta(seconds=cognition_config.focus_ttl_seconds + 1),
        )


def test_memory_scope_and_outcome_matrix(cognition_config: CognitionConfig) -> None:
    with pytest.raises(ValueError, match="viewer_ref"):
        MemoryProposal(
            config=cognition_config, schema_version=1, proposal_id="memory-p1",
            context_id=CONTEXT_ID, kind=MemoryKind.PREFERENCE, content="Likes blue",
            scope=MemoryScope.VIEWER, viewer_ref=None,
            claim_basis=MemoryClaimBasis.OBSERVED_INPUT,
            provenance_refs=("event-1",), outcome_ref=None, confidence=0.7,
            retention_class=MemoryRetentionClass.SESSION,
        )
    with pytest.raises(ValueError, match="outcome_ref"):
        MemoryProposal(
            config=cognition_config, schema_version=1, proposal_id="memory-p2",
            context_id=CONTEXT_ID, kind=MemoryKind.EPISODIC, content="Scene changed",
            scope=MemoryScope.SESSION, viewer_ref=None,
            claim_basis=MemoryClaimBasis.VERIFIED_ACTION,
            provenance_refs=("event-1",), outcome_ref=None, confidence=0.9,
            retention_class=MemoryRetentionClass.PERSISTENT_CANDIDATE,
        )


def test_context_and_proposal_reference_mismatch_fails(cognition_config: CognitionConfig) -> None:
    context = _context(cognition_config)
    with pytest.raises(ValueError, match="stale or mismatched"):
        CognitiveTurn(
            config=cognition_config, context=context, schema_version=1,
            turn_id="turn-stale", context_id="b" * 64, mode=CognitiveMode.WAIT,
            attention_target_id=None, intent=None, speech_text=None,
            action_proposal=None, focus_proposal=None, memory_proposals=(),
            evidence_refs=("event-1",), uncertainty=CognitiveUncertainty.UNKNOWN,
            reason_codes=("intentional_wait",),
        )


@pytest.mark.asyncio
async def test_cognitive_feature_can_enable_only_through_attached_handler(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_bytes((ROOT / "config" / "system.yaml").read_bytes())
    feature_path = tmp_path / "features.yaml"
    feature_path.write_bytes((ROOT / "config" / "features.yaml").read_bytes())
    loader = ConfigLoader(tmp_path)
    loader.load_all()
    metrics = MetricsCollector()
    manager = FeatureManager.from_config(loader, persist=True, metrics=metrics)
    before = feature_path.read_bytes()
    handler_called = False

    async def enable_handler() -> None:
        nonlocal handler_called
        handler_called = True

    manager.attach_handlers("cognitive_brain_shadow", enable=enable_handler)
    result = await manager.enable("cognitive_brain_shadow", user="owner")
    assert result.ok is True
    assert await manager.get_status("cognitive_brain_shadow") is FeatureStatus.ENABLED
    assert handler_called is True
    assert feature_path.read_bytes() != before


def test_cognitive_feature_can_start_enabled_but_cannot_coerce_activation(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_bytes((ROOT / "config" / "system.yaml").read_bytes())
    raw = yaml.safe_load((ROOT / "config" / "features.yaml").read_text(encoding="utf-8"))
    raw["features"]["cognitive_brain_shadow"]["enabled"] = True
    (tmp_path / "features.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    loader = ConfigLoader(tmp_path)
    loader.load_all()
    manager = FeatureManager.from_config(loader)
    assert manager is not None

    raw["features"]["cognitive_brain_shadow"]["enabled"] = False
    raw["features"]["cognitive_brain_shadow"]["activation_allowed"] = "false"
    (tmp_path / "features.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    loader.load_all()
    with pytest.raises(ConfigError, match="boolean"):
        FeatureManager.from_config(loader)


def test_cognition_metrics_reject_unbounded_labels() -> None:
    metrics = MetricsCollector()
    metrics.record_cognitive_contract_rejected("invalid_reference")
    with pytest.raises(ValueError, match="unsupported"):
        metrics.record_cognitive_contract_rejected("raw viewer id")
    with pytest.raises(ValueError, match="unsupported"):
        metrics.record_cognitive_feature_toggle("arbitrary")
    assert metrics.cognition_snapshot()["contract_rejected"] == {"invalid_reference": 1}
    rendered = metrics.prometheus_text().decode("utf-8")
    assert 'cognitive_contract_rejected_total{reason="invalid_reference"} 1.0' in rendered


def test_interface_has_no_concrete_service_import() -> None:
    source = (ROOT / "interfaces" / "cognition.py").read_text(encoding="utf-8")
    assert "from services." not in source
    assert "import services." not in source
