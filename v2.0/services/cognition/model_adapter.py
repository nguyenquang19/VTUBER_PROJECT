"""Single prompt/model boundary for proposal-only Cognitive Brain generation."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveBrainParseError,
    CognitiveBrainSchemaError,
    CognitiveContext,
    CognitiveMode,
    CognitiveModelAdapterService,
    CognitiveModelBusyError,
    CognitiveModelContextError,
    CognitiveModelOutput,
    CognitiveModelPreemptedError,
    CognitiveModelTelemetry,
    CognitiveModelTimeoutError,
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
from services.llm.llama_cpp_llm import (
    LlamaCppBusyError,
    LlamaCppContextBudgetError,
    LlamaCppPreemptedError,
)
from services.llm.prompt_cache import PromptCache


_REPO_ROOT = Path(__file__).resolve().parents[2]


class CognitiveModelAdapter(CognitiveModelAdapterService):
    """Own one strict llama.cpp request; no parsing, decision, or side effect."""

    service_id = "cognitive_model_adapter"

    def __init__(
        self,
        *,
        config: CognitionConfig,
        llm: LLMService,
        persona_prompt: str,
        brain_prompt: str,
    ) -> None:
        persona = persona_prompt.strip()
        prompt = brain_prompt.strip()
        if not persona or not prompt:
            raise ValueError("Brain persona and prompt must be non-empty")
        self._config = config
        self._llm = llm
        self._system_prompt = f"{persona}\n\n{prompt}"
        self._running = False
        self._active_request_id: str | None = None
        self._last_request_id: str | None = None
        self._last_telemetry: CognitiveModelTelemetry | None = None
        self._calls = 0
        self._failures = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        llm: LLMService,
        config: CognitionConfig,
    ) -> "CognitiveModelAdapter":
        persona = PromptCache.from_loader(loader).text
        configured = Path(config.brain_prompt_path)
        prompt_path = configured if configured.is_absolute() else _REPO_ROOT / configured
        if not prompt_path.is_file():
            raise ValueError(f"Brain shadow prompt does not exist: {prompt_path}")
        return cls(
            config=config,
            llm=llm,
            persona_prompt=persona,
            brain_prompt=prompt_path.read_text(encoding="utf-8"),
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        await self.cancel_active()
        self._running = False
        self._last_telemetry = None
        self._last_request_id = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            active=self._active_request_id is not None,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "cognitive_model_adapter_running": self._running,
            "cognitive_model_adapter_active": self._active_request_id is not None,
            "cognitive_model_adapter_calls_total": self._calls,
            "cognitive_model_adapter_failures_total": self._failures,
        }

    @property
    def last_telemetry(self) -> CognitiveModelTelemetry | None:
        return self._last_telemetry

    async def cancel_active(self) -> None:
        request_id = self._active_request_id
        if request_id is not None:
            await self._llm.cancel(request_id)

    async def reject_last_output(self) -> None:
        request_id = self._last_request_id
        if request_id is not None:
            await self._llm.cancel(request_id)

    async def generate(self, context: CognitiveContext) -> CognitiveModelOutput:
        return await self._generate(context, workload_class=LLMWorkloadClass.SHADOW)

    async def generate_public(self, context: CognitiveContext) -> CognitiveModelOutput:
        return await self._generate(context, workload_class=LLMWorkloadClass.LIVE)

    async def _generate(
        self,
        context: CognitiveContext,
        *,
        workload_class: LLMWorkloadClass,
    ) -> CognitiveModelOutput:
        if not self._running:
            raise RuntimeError("Cognitive model adapter is not running")
        if self._active_request_id is not None:
            raise RuntimeError("Cognitive model adapter already has an active request")
        request_id = _request_id(context.context_id)
        self._last_request_id = request_id
        request = LLMRequest(
            request_id=request_id,
            messages=[
                ChatMessage(role="system", content=self._system_prompt),
                ChatMessage(
                    role="user",
                    content=_canonical_json(_brain_decision_view(context)),
                ),
            ],
            max_tokens=self._config.brain_max_output_tokens,
            temperature=self._config.brain_temperature,
            workload_class=workload_class,
            context_overflow_policy=LLMContextOverflowPolicy.REJECT,
            response_format=LLMJsonSchemaResponse(
                name="mai_cognitive_shadow_turn",
                schema=_response_schema(context, self._config),
            ),
        )
        self._active_request_id = request_id
        self._calls += 1
        started = time.perf_counter()
        first: float | None = None
        chunks: list[str] = []
        input_tokens: int | None = None
        output_tokens = 0
        saw_final = False

        async def consume() -> None:
            nonlocal first, input_tokens, output_tokens, saw_final
            async for token in self._llm.generate_stream(request):
                if token.is_final:
                    if saw_final:
                        raise CognitiveBrainParseError(
                            "Brain stream emitted multiple final tokens"
                        )
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
            await asyncio.wait_for(
                consume(), timeout=self._config.brain_timeout_seconds,
            )
            if not saw_final:
                raise CognitiveBrainParseError(
                    "Brain stream ended without a final token"
                )
            raw = "".join(chunks)
            return CognitiveModelOutput(
                raw_output=raw,
                telemetry=_telemetry(
                    request_id,
                    started,
                    first,
                    input_tokens,
                    output_tokens,
                ),
            )
        except LlamaCppBusyError as exc:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise CognitiveModelBusyError(str(exc)) from exc
        except LlamaCppContextBudgetError as exc:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise CognitiveModelContextError(str(exc)) from exc
        except LlamaCppPreemptedError as exc:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise CognitiveModelPreemptedError(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise CognitiveModelTimeoutError("Brain generation timed out") from exc
        except BaseException:
            self._failures += 1
            await self._llm.cancel(request_id)
            raise
        finally:
            self._last_telemetry = _telemetry(
                request_id,
                started,
                first,
                input_tokens,
                output_tokens,
            )
            self._active_request_id = None


def _telemetry(
    request_id: str,
    started: float,
    first: float | None,
    input_tokens: int | None,
    output_tokens: int,
) -> CognitiveModelTelemetry:
    ended = time.perf_counter()
    return CognitiveModelTelemetry(
        request_id=request_id,
        ttft_ms=None if first is None else (first - started) * 1000.0,
        generation_ms=(ended - started) * 1000.0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _request_id(context_id: str) -> str:
    return f"brain:{context_id.removeprefix('ctx:')[:64]}"


def _response_schema(
    context: CognitiveContext,
    config: CognitionConfig,
) -> dict[str, Any]:
    evidence_refs = sorted(_context_refs(context))
    attention_ids = sorted(
        item.evidence_id
        for item in (
            *((context.chat_digest,) if context.chat_digest is not None else ()),
            *context.attention_items,
        )
    )
    nullable_attention: dict[str, Any] = {"anyOf": [{"type": "null"}]}
    if attention_ids:
        nullable_attention["anyOf"].append(
            {"type": "string", "enum": attention_ids}
        )
    refs_schema: dict[str, Any] = {
        "type": "array",
        "maxItems": config.max_evidence_refs if evidence_refs else 0,
        "uniqueItems": True,
        "items": (
            {"type": "string", "enum": evidence_refs}
            if evidence_refs
            else {"type": "string"}
        ),
    }
    shared = {
        "evidence_refs": refs_schema,
        "uncertainty": {
            "type": "string",
            "enum": [item.value for item in CognitiveUncertainty],
        },
        "reason_codes": {
            "type": "array",
            "minItems": 1,
            "maxItems": config.max_reason_codes,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(config.reason_codes)},
        },
    }

    def branch(mode: str) -> dict[str, Any]:
        wait = mode == "WAIT"
        properties = {
            "mode": {"type": "string", "const": mode},
            "attention_target_id": (
                {"type": "null"} if wait else nullable_attention
            ),
            "intent": (
                {"type": "null"}
                if wait
                else {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": config.max_brain_intent_chars,
                }
            ),
            "speech_text": (
                {"type": "null"}
                if wait
                else {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": config.max_brain_speech_chars,
                }
            ),
            **shared,
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(properties),
            "properties": properties,
        }

    modes = [
        mode.value
        for mode in context.available_modes
        if mode in (CognitiveMode.WAIT, CognitiveMode.SPEAK)
    ]
    if not modes:
        raise CognitiveBrainSchemaError(
            "context has no MCB-3 response mode", code="mode_unavailable",
        )
    return {"oneOf": [branch(mode) for mode in modes]}


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


def _brain_decision_view(context: CognitiveContext) -> dict[str, Any]:
    def evidence(item: Any) -> dict[str, Any]:
        return {
            "evidence_id": item.evidence_id,
            "source": item.source.value,
            "summary": item.summary,
            "provenance_refs": list(item.provenance_refs),
            "observed_at": item.observed_at.isoformat(),
            "expires_at": (
                None if item.expires_at is None else item.expires_at.isoformat()
            ),
        }

    hard = context.operator_state
    conversation = context.conversation_state
    return {
        "context_id": context.context_id,
        "created_at": context.created_at.isoformat(),
        "hard_state": {
            "emergency": hard.emergency,
            "operator_hold": hard.operator_hold,
            "safety_hold": hard.safety_hold,
            "permission_hold": hard.permission_hold,
            "transaction_conflict": hard.transaction_conflict,
            "critical_state": hard.critical_state,
            "source_failure_codes": list(hard.source_failure_codes),
        },
        "available_modes": [item.value for item in context.available_modes],
        "chat_digest": (
            None if context.chat_digest is None else evidence(context.chat_digest)
        ),
        "attention_items": [evidence(item) for item in context.attention_items],
        "conversation_state": {
            "topic": conversation.topic,
            "thread_ref": conversation.thread_ref,
            "goal_ref": conversation.goal_ref,
            "intention_ref": conversation.intention_ref,
            "summary": conversation.summary,
            "evidence_refs": list(conversation.evidence_refs),
        },
        "memory_items": [
            {
                "memory_ref": item.memory_ref,
                "kind": item.kind.value,
                "summary": item.summary,
                "scope": item.scope.value,
                "viewer_ref": item.viewer_ref,
                "provenance_refs": list(item.provenance_refs),
                "observed_at": item.observed_at.isoformat(),
                "expires_at": (
                    None if item.expires_at is None else item.expires_at.isoformat()
                ),
                "confidence": item.confidence,
            }
            for item in context.memory_items
        ],
        "recent_delivered_speech": [
            {
                "delivery_id": item.delivery_id,
                "speech_text": item.speech_text,
                "delivered_at": item.delivered_at.isoformat(),
                "source_mode": item.source_mode,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in context.recent_delivered_speech
        ],
    }


def _wire(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _wire(getattr(value, field.name)) for field in fields(value)
        }
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
        _wire(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
