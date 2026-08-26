"""Strict structured-output behavior for the proposal-only shadow Brain."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveContext,
    CognitiveConversationState,
    CognitiveEvidenceItem,
    CognitiveEvidenceSource,
    CognitiveHardState,
    CognitiveMode,
)
from interfaces.llm import LLMRequest, LLMToken
from services.cognition.brain import (
    CognitiveBrain,
    CognitiveBrainParseError,
    CognitiveBrainSchemaError,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _config() -> CognitionConfig:
    raw = yaml.safe_load((ROOT / "config" / "cognition.yaml").read_text(encoding="utf-8"))
    return CognitionConfig.from_mapping(raw)


def _context(config: CognitionConfig) -> CognitiveContext:
    evidence = CognitiveEvidenceItem(
        config=config, schema_version=1, evidence_id="agent:chat:m1",
        source=CognitiveEvidenceSource.CHAT, summary="Mai thích cà phê không?",
        provenance_refs=("m1",), observed_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    return CognitiveContext(
        config=config, schema_version=1, context_id="c" * 64,
        created_at=NOW, session_id="stream:test", world_snapshot_id="world:1",
        self_snapshot_id="self:1", capability_snapshot_id="capability:1",
        focus_snapshot_id=None,
        operator_state=CognitiveHardState(
            config=config, schema_version=1, emergency=False,
            operator_hold=False, safety_hold=False, permission_hold=False,
            transaction_conflict=False, critical_state=False,
            source_failure_codes=(),
        ),
        available_modes=(CognitiveMode.WAIT, CognitiveMode.SPEAK),
        available_actions=(), chat_digest=evidence, attention_items=(),
        conversation_state=CognitiveConversationState(
            config=config, schema_version=1, topic="cà phê", thread_ref=None,
            goal_ref=None, intention_ref=None, summary="viewer hỏi về cà phê",
            evidence_refs=("agent:chat:m1",),
        ),
        memory_items=(), recent_delivered_speech=(),
    )


class _LLM:
    service_id = "fake_llm"

    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[LLMRequest] = []
        self.cancelled: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {}

    async def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    async def generate_stream(self, request: LLMRequest):
        self.requests.append(request)
        yield LLMToken(request_id=request.request_id, token=self.output, is_final=False)
        yield LLMToken(
            request_id=request.request_id, token="", is_final=True,
            metadata={"input_tokens": 120, "tokens_predicted": 20},
        )


def _speak_json() -> str:
    return (
        '{"mode":"SPEAK","attention_target_id":"agent:chat:m1",'
        '"intent":"trả lời ngắn","speech_text":"Có chứ, nhất là cà phê đậm.",'
        '"evidence_refs":["agent:chat:m1"],"uncertainty":"LOW",'
        '"reason_codes":["propose_speech"]}'
    )


@pytest.mark.asyncio
async def test_brain_uses_shadow_reject_schema_and_returns_no_side_effect_proposals() -> None:
    config = _config()
    llm = _LLM(_speak_json())
    brain = CognitiveBrain(
        config=config, llm=llm, persona_prompt="Mai persona",
        shadow_prompt="Return strict JSON.",
    )
    await brain.start()
    first = await brain.propose(_context(config))
    second = await brain.propose(_context(config))
    assert first.mode is CognitiveMode.SPEAK
    assert first.turn_id == second.turn_id
    assert first.action_proposal is None
    assert first.focus_proposal is None
    assert first.memory_proposals == ()
    request = llm.requests[0]
    assert request.workload_class.value == "shadow"
    assert request.context_overflow_policy.value == "reject"
    assert request.response_format is not None
    assert request.response_format.to_payload()["json_schema"]["strict"] is True
    assert "compatibility" not in request.messages[-1].content
    assert "world_snapshot_id" not in request.messages[-1].content
    assert "chat_digest" in request.messages[-1].content
    schema = request.response_format.schema
    assert [item["properties"]["mode"]["const"] for item in schema["oneOf"]] == [
        "WAIT", "SPEAK",
    ]
    assert schema["oneOf"][0]["properties"]["speech_text"] == {"type": "null"}
    assert schema["oneOf"][1]["properties"]["speech_text"]["maxLength"] == (
        config.max_brain_speech_chars
    )
    assert brain.last_telemetry is not None
    assert brain.last_telemetry.input_tokens == 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "error"),
    [
        (_speak_json() + " trailing", CognitiveBrainParseError),
        (_speak_json().replace('"mode":"SPEAK"', '"mode":"SPEAK","mode":"WAIT"'), CognitiveBrainParseError),
        (_speak_json().replace('"reason_codes"', '"unknown":1,"reason_codes"'), CognitiveBrainSchemaError),
        (_speak_json().replace("agent:chat:m1", "stale:ref"), CognitiveBrainSchemaError),
    ],
)
async def test_brain_rejects_non_exact_or_stale_output_without_retry(
    output: str, error: type[Exception],
) -> None:
    config = _config()
    llm = _LLM(output)
    brain = CognitiveBrain(
        config=config, llm=llm, persona_prompt="Mai persona", shadow_prompt="strict",
    )
    await brain.start()
    with pytest.raises(error):
        await brain.propose(_context(config))
    assert len(llm.requests) == 1
    assert len(llm.cancelled) == 1


@pytest.mark.asyncio
async def test_wait_requires_null_content_and_never_invents_safe_speech() -> None:
    config = _config()
    output = (
        '{"mode":"WAIT","attention_target_id":null,"intent":null,'
        '"speech_text":"fallback","evidence_refs":[],"uncertainty":"UNKNOWN",'
        '"reason_codes":["intentional_wait"]}'
    )
    brain = CognitiveBrain(
        config=config, llm=_LLM(output), persona_prompt="Mai", shadow_prompt="strict",
    )
    await brain.start()
    with pytest.raises(CognitiveBrainSchemaError, match="WAIT"):
        await brain.propose(_context(config))


@pytest.mark.asyncio
async def test_brain_accepts_outer_json_whitespace_but_not_non_whitespace_suffix() -> None:
    config = _config()
    brain = CognitiveBrain(
        config=config, llm=_LLM(f"\r\n  {_speak_json()}  \n"),
        persona_prompt="Mai", shadow_prompt="strict",
    )
    await brain.start()
    assert (await brain.propose(_context(config))).mode is CognitiveMode.SPEAK


@pytest.mark.asyncio
async def test_brain_canonicalizes_intent_and_speech_outer_whitespace() -> None:
    config = _config()
    output = _speak_json().replace(
        '"intent":"trả lời ngắn","speech_text":"Có chứ, nhất là cà phê đậm."',
        '"intent":"  trả lời ngắn  ","speech_text":"  Có chứ, nhất là cà phê đậm.  "',
    )
    brain = CognitiveBrain(
        config=config, llm=_LLM(output), persona_prompt="Mai", shadow_prompt="strict",
    )
    await brain.start()
    turn = await brain.propose(_context(config))
    assert turn.intent == "trả lời ngắn"
    assert turn.speech_text == "Có chứ, nhất là cà phê đậm."


@pytest.mark.asyncio
async def test_brain_deduplicates_set_arrays_before_turn_identity() -> None:
    config = _config()
    canonical = CognitiveBrain(
        config=config, llm=_LLM(_speak_json()),
        persona_prompt="Mai", shadow_prompt="strict",
    )
    duplicated_output = _speak_json().replace(
        '"evidence_refs":["agent:chat:m1"]',
        '"evidence_refs":["agent:chat:m1","agent:chat:m1"]',
    ).replace(
        '"reason_codes":["propose_speech"]',
        '"reason_codes":["propose_speech","propose_speech"]',
    )
    duplicated = CognitiveBrain(
        config=config, llm=_LLM(duplicated_output),
        persona_prompt="Mai", shadow_prompt="strict",
    )
    await canonical.start()
    await duplicated.start()
    first = await canonical.propose(_context(config))
    second = await duplicated.propose(_context(config))
    assert second.evidence_refs == ("agent:chat:m1",)
    assert second.reason_codes == ("propose_speech",)
    assert second.turn_id == first.turn_id


@pytest.mark.asyncio
async def test_brain_schema_exposes_only_modes_available_in_context() -> None:
    config = _config()
    context = replace(
        _context(config), config=config, available_modes=(CognitiveMode.WAIT,),
    )
    output = (
        '{"mode":"WAIT","attention_target_id":null,"intent":null,'
        '"speech_text":null,"evidence_refs":[],"uncertainty":"UNKNOWN",'
        '"reason_codes":["intentional_wait"]}'
    )
    llm = _LLM(output)
    brain = CognitiveBrain(
        config=config, llm=llm, persona_prompt="Mai", shadow_prompt="strict",
    )
    await brain.start()
    assert (await brain.propose(context)).mode is CognitiveMode.WAIT
    schema = llm.requests[0].response_format.schema
    assert [item["properties"]["mode"]["const"] for item in schema["oneOf"]] == [
        "WAIT",
    ]
