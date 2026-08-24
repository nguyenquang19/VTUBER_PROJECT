"""Strict proposal-only Cognitive Brain adapter for MCB-3 shadow observation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveBrainService,
    CognitiveContext,
    CognitiveMode,
    CognitiveTurn,
    CognitiveUncertainty,
)
from interfaces.llm import (
    ChatMessage,
    LLMContextOverflowPolicy,
    LLMJsonSchemaResponse,
    LLMRequest,
    LLMService,
    LLMWorkloadClass,
)
from services.llm.prompt_cache import PromptCache


_REPO_ROOT = Path(__file__).resolve().parents[2]
_OUTPUT_KEYS = frozenset({
    "mode", "attention_target_id", "intent", "speech_text",
    "evidence_refs", "uncertainty", "reason_codes",
})


class CognitiveBrainParseError(ValueError):
    """The model output was not exactly one JSON object."""


class CognitiveBrainSchemaError(ValueError):
    """The model output failed the strict cognitive contract."""


@dataclass(frozen=True)
class BrainTelemetry:
    request_id: str
    ttft_ms: float | None
    generation_ms: float
    input_tokens: int | None
    output_tokens: int


class CognitiveBrain(CognitiveBrainService):
    """One-call, no-history Brain that cannot execute its proposal."""

    service_id = "cognitive_brain_shadow"

    def __init__(
        self,
        *,
        config: CognitionConfig,
        llm: LLMService,
        persona_prompt: str,
        shadow_prompt: str,
    ) -> None:
        persona = persona_prompt.strip()
        shadow = shadow_prompt.strip()
        if not persona or not shadow:
            raise ValueError("Brain persona and shadow prompts must be non-empty")
        self._config = config
        self._llm = llm
        self._system_prompt = f"{persona}\n\n{shadow}"
        self._running = False
        self._active_request_id: str | None = None
        self._last_telemetry: BrainTelemetry | None = None
        self._calls = 0
        self._proposals = 0
        self._failures = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, llm: LLMService, config: CognitionConfig,
    ) -> "CognitiveBrain":
        persona = PromptCache.from_loader(loader).text
        configured = Path(config.brain_prompt_path)
        prompt_path = configured if configured.is_absolute() else _REPO_ROOT / configured
        if not prompt_path.is_file():
            raise ValueError(f"Brain shadow prompt does not exist: {prompt_path}")
        return cls(
            config=config,
            llm=llm,
            persona_prompt=persona,
            shadow_prompt=prompt_path.read_text(encoding="utf-8"),
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        request_id = self._active_request_id
        self._running = False
        if request_id is not None:
            await self._llm.cancel(request_id)
        self._active_request_id = None
        self._last_telemetry = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, active=self._active_request_id is not None,
        )

    def get_metrics(self) -> dict[str, Any]:
        telemetry = self._last_telemetry
        return {
            "cognitive_brain_calls_total": self._calls,
            "cognitive_brain_proposals_total": self._proposals,
            "cognitive_brain_failures_total": self._failures,
            "cognitive_brain_active": self._active_request_id is not None,
            "cognitive_brain_last_ttft_ms": None if telemetry is None else telemetry.ttft_ms,
            "cognitive_brain_last_generation_ms": (
                None if telemetry is None else telemetry.generation_ms
            ),
        }

    @property
    def last_telemetry(self) -> BrainTelemetry | None:
        return self._last_telemetry

    async def cancel_active(self) -> None:
        request_id = self._active_request_id
        if request_id is not None:
            await self._llm.cancel(request_id)

    async def propose(self, context: CognitiveContext) -> CognitiveTurn:
        if not self._running:
            raise RuntimeError("Cognitive Brain is not running")
        if self._active_request_id is not None:
            raise RuntimeError("Cognitive Brain already has an active request")
        request_id = _request_id(context.context_id)
        request = LLMRequest(
            request_id=request_id,
            messages=[
                ChatMessage(role="system", content=self._system_prompt),
                ChatMessage(role="user", content=_canonical_json(context)),
            ],
            max_tokens=self._config.brain_max_output_tokens,
            temperature=self._config.brain_temperature,
            workload_class=LLMWorkloadClass.SHADOW,
            context_overflow_policy=LLMContextOverflowPolicy.REJECT,
            response_format=LLMJsonSchemaResponse(
                name="mai_cognitive_shadow_turn",
                schema=_response_schema(context, self._config),
            ),
        )
        self._active_request_id = request_id
        self._calls += 1
        started = time.perf_counter()
        first = None
        chunks: list[str] = []
        input_tokens: int | None = None
        output_tokens = 0
        saw_final = False

        async def consume() -> None:
            nonlocal first, input_tokens, output_tokens, saw_final
            async for token in self._llm.generate_stream(request):
                if token.is_final:
                    if saw_final:
                        raise CognitiveBrainParseError("Brain stream emitted multiple final tokens")
                    saw_final = True
                    raw_input = token.metadata.get("input_tokens")
                    if isinstance(raw_input, int) and not isinstance(raw_input, bool):
                        input_tokens = raw_input
                    raw_output = token.metadata.get("tokens_predicted")
                    if isinstance(raw_output, int) and not isinstance(raw_output, bool):
                        output_tokens = raw_output
                    continue
                if first is None:
                    first = time.perf_counter()
                chunks.append(token.token)

        try:
            await asyncio.wait_for(consume(), timeout=self._config.brain_timeout_seconds)
            if not saw_final:
                raise CognitiveBrainParseError("Brain stream ended without a final token")
            raw = "".join(chunks)
            payload = _parse_exact_object(raw)
            turn = _materialize_turn(payload, context, self._config)
            self._proposals += 1
            return turn
        except BaseException:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise
        finally:
            ended = time.perf_counter()
            self._last_telemetry = BrainTelemetry(
                request_id=request_id,
                ttft_ms=None if first is None else (first - started) * 1000.0,
                generation_ms=(ended - started) * 1000.0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self._active_request_id = None


def _request_id(context_id: str) -> str:
    return f"brain:{context_id.removeprefix('ctx:')[:64]}"


def _response_schema(context: CognitiveContext, config: CognitionConfig) -> dict[str, Any]:
    evidence_refs = sorted(_context_refs(context))
    attention_ids = sorted(
        item.evidence_id for item in (
            *((context.chat_digest,) if context.chat_digest is not None else ()),
            *context.attention_items,
        )
    )
    nullable_attention: dict[str, Any] = {
        "anyOf": [{"type": "null"}],
    }
    if attention_ids:
        nullable_attention["anyOf"].append({"type": "string", "enum": attention_ids})
    refs_schema: dict[str, Any] = {
        "type": "array",
        "maxItems": config.max_evidence_refs if evidence_refs else 0,
        "uniqueItems": True,
        "items": (
            {"type": "string", "enum": evidence_refs}
            if evidence_refs else {"type": "string"}
        ),
    }
    properties = {
        "mode": {"type": "string", "enum": ["WAIT", "SPEAK"]},
        "attention_target_id": nullable_attention,
        "intent": {"anyOf": [
            {"type": "null"},
            {"type": "string", "minLength": 1, "maxLength": config.max_brain_intent_chars},
        ]},
        "speech_text": {"anyOf": [
            {"type": "null"},
            {"type": "string", "minLength": 1, "maxLength": config.max_speech_chars},
        ]},
        "evidence_refs": refs_schema,
        "uncertainty": {"type": "string", "enum": [item.value for item in CognitiveUncertainty]},
        "reason_codes": {
            "type": "array", "minItems": 1, "maxItems": config.max_reason_codes,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(config.reason_codes)},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_OUTPUT_KEYS),
        "properties": properties,
    }


def _parse_exact_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise CognitiveBrainParseError("Brain output must be one trimmed JSON object")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise CognitiveBrainParseError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (json.JSONDecodeError, CognitiveBrainParseError) as exc:
        raise CognitiveBrainParseError("Brain output is not exact JSON") from exc
    if not isinstance(value, dict):
        raise CognitiveBrainParseError("Brain output must be a JSON object")
    if set(value) != _OUTPUT_KEYS:
        raise CognitiveBrainSchemaError("Brain output keys do not match the contract")
    return value


def _materialize_turn(
    value: Mapping[str, Any], context: CognitiveContext, config: CognitionConfig,
) -> CognitiveTurn:
    try:
        mode = CognitiveMode(value["mode"])
        if mode not in (CognitiveMode.WAIT, CognitiveMode.SPEAK):
            raise ValueError("MCB-3 allows only WAIT or SPEAK")
        uncertainty = CognitiveUncertainty(value["uncertainty"])
        evidence_refs = _strict_string_tuple(value["evidence_refs"], "evidence_refs")
        reason_codes = _strict_string_tuple(value["reason_codes"], "reason_codes")
        target = _nullable_string(value["attention_target_id"], "attention_target_id")
        intent = _nullable_string(value["intent"], "intent")
        speech = _nullable_string(value["speech_text"], "speech_text")
        if intent is not None and len(intent) > config.max_brain_intent_chars:
            raise ValueError("intent exceeds Brain bound")
        digest = hashlib.sha256(
            f"{context.context_id}\n{_canonical_json(value)}".encode("utf-8")
        ).hexdigest()[:64]
        return CognitiveTurn(
            config=config,
            context=context,
            schema_version=config.schema_version,
            turn_id=f"brain-turn:{digest}",
            context_id=context.context_id,
            mode=mode,
            attention_target_id=target,
            intent=intent,
            speech_text=speech,
            action_proposal=None,
            focus_proposal=None,
            memory_proposals=(),
            evidence_refs=evidence_refs,
            uncertainty=uncertainty,
            reason_codes=reason_codes,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CognitiveBrainSchemaError):
            raise
        raise CognitiveBrainSchemaError(str(exc)) from exc


def _nullable_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be null or a trimmed non-empty string")
    return value


def _strict_string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must contain only strings")
    return tuple(value)


def _context_refs(context: CognitiveContext) -> set[str]:
    refs = set(context.conversation_state.evidence_refs)
    items = list(context.attention_items)
    if context.chat_digest is not None:
        items.append(context.chat_digest)
    for item in items:
        refs.add(item.evidence_id)
        refs.update(item.provenance_refs)
    for item in context.memory_items:
        refs.add(item.memory_ref)
        refs.update(item.provenance_refs)
    for item in context.recent_delivered_speech:
        refs.add(item.delivery_id)
        refs.update(item.evidence_refs)
    for item in context.available_actions:
        refs.add(item.availability_ref)
        refs.update(item.evidence_refs)
    return refs


def _wire(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("cognitive context contains an unsupported value")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _wire(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
