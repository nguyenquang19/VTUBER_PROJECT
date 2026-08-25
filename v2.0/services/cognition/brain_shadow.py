"""Strict proposal-only Cognitive Brain adapter for MCB-3 shadow observation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveBrainParseError,
    CognitiveBrainService,
    CognitiveBrainSchemaError,
    CognitiveContext,
    CognitiveModelAdapterService,
    CognitiveModelTelemetry,
    CognitiveMode,
    CognitiveTurn,
    CognitiveUncertainty,
)
from interfaces.llm import LLMService
from services.cognition.model_adapter import CognitiveModelAdapter


_OUTPUT_KEYS = frozenset({
    "mode", "attention_target_id", "intent", "speech_text",
    "evidence_refs", "uncertainty", "reason_codes",
})


BrainTelemetry = CognitiveModelTelemetry


class CognitiveBrain(CognitiveBrainService):
    """One-call, no-history Brain that cannot execute its proposal."""

    service_id = "cognitive_brain_shadow"

    def __init__(
        self,
        *,
        config: CognitionConfig,
        model_adapter: CognitiveModelAdapterService | None = None,
        llm: LLMService | None = None,
        persona_prompt: str | None = None,
        shadow_prompt: str | None = None,
    ) -> None:
        self._config = config
        if model_adapter is None:
            if llm is None or persona_prompt is None or shadow_prompt is None:
                raise ValueError("Brain requires one CognitiveModelAdapter")
            model_adapter = CognitiveModelAdapter(
                config=config,
                llm=llm,
                persona_prompt=persona_prompt,
                brain_prompt=shadow_prompt,
            )
        if not isinstance(model_adapter, CognitiveModelAdapterService):
            raise ValueError("model_adapter must implement CognitiveModelAdapterService")
        self._model_adapter = model_adapter
        self._running = False
        self._last_telemetry: CognitiveModelTelemetry | None = None
        self._calls = 0
        self._proposals = 0
        self._failures = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        llm: LLMService,
        config: CognitionConfig,
        model_adapter: CognitiveModelAdapterService | None = None,
    ) -> "CognitiveBrain":
        return cls(
            config=config,
            model_adapter=(
                model_adapter
                or CognitiveModelAdapter.from_loader(
                    loader, llm=llm, config=config,
                )
            ),
        )

    async def start(self) -> None:
        await self._model_adapter.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self._model_adapter.stop()
        self._last_telemetry = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        adapter_health = await self._model_adapter.health_check()
        if not adapter_health.is_ok:
            return HealthStatus.degraded(
                self.service_id, "cognitive model adapter is unavailable",
            )
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        telemetry = self._last_telemetry
        adapter_metrics = self._model_adapter.get_metrics()
        return {
            "cognitive_brain_calls_total": self._calls,
            "cognitive_brain_proposals_total": self._proposals,
            "cognitive_brain_failures_total": self._failures,
            "cognitive_brain_active": bool(
                adapter_metrics.get("cognitive_model_adapter_active", False)
            ),
            "cognitive_brain_last_ttft_ms": None if telemetry is None else telemetry.ttft_ms,
            "cognitive_brain_last_generation_ms": (
                None if telemetry is None else telemetry.generation_ms
            ),
        }

    @property
    def last_telemetry(self) -> BrainTelemetry | None:
        return self._last_telemetry

    async def cancel_active(self) -> None:
        await self._model_adapter.cancel_active()

    async def propose(self, context: CognitiveContext) -> CognitiveTurn:
        if not self._running:
            raise RuntimeError("Cognitive Brain is not running")
        self._calls += 1
        try:
            generated = await self._model_adapter.generate(context)
            self._last_telemetry = generated.telemetry
            payload = _parse_exact_object(generated.raw_output)
            turn = _materialize_turn(payload, context, self._config)
            self._proposals += 1
            return turn
        except (CognitiveBrainParseError, CognitiveBrainSchemaError, ValueError):
            self._failures += 1
            await self._model_adapter.reject_last_output()
            telemetry = getattr(self._model_adapter, "last_telemetry", None)
            if isinstance(telemetry, CognitiveModelTelemetry):
                self._last_telemetry = telemetry
            raise
        except BaseException:
            self._failures += 1
            telemetry = getattr(self._model_adapter, "last_telemetry", None)
            if isinstance(telemetry, CognitiveModelTelemetry):
                self._last_telemetry = telemetry
            raise


def _parse_exact_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise CognitiveBrainParseError("Brain output must be one JSON object")
    raw = raw.strip()

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
        raise CognitiveBrainSchemaError(
            "Brain output keys do not match the contract", code="key_set",
        )
    return value


def _materialize_turn(
    value: Mapping[str, Any], context: CognitiveContext, config: CognitionConfig,
) -> CognitiveTurn:
    try:
        mode = CognitiveMode(value["mode"])
        if mode not in (CognitiveMode.WAIT, CognitiveMode.SPEAK):
            raise ValueError("MCB-3 allows only WAIT or SPEAK")
        uncertainty = CognitiveUncertainty(value["uncertainty"])
        evidence_refs = _strict_string_tuple(
            value["evidence_refs"], "evidence_refs", deduplicate=True,
        )
        reason_codes = _strict_string_tuple(
            value["reason_codes"], "reason_codes", deduplicate=True,
        )
        target = _nullable_string(value["attention_target_id"], "attention_target_id")
        intent = _nullable_string(value["intent"], "intent")
        speech = _nullable_string(value["speech_text"], "speech_text")
        if intent is not None and len(intent) > config.max_brain_intent_chars:
            raise ValueError("intent exceeds Brain bound")
        if speech is not None and len(speech) > config.max_brain_speech_chars:
            raise ValueError("speech_text exceeds Brain bound")
        normalized = dict(value)
        normalized.update({
            "attention_target_id": target,
            "intent": intent,
            "speech_text": speech,
            "evidence_refs": list(evidence_refs),
            "reason_codes": list(reason_codes),
        })
        digest = hashlib.sha256(
            f"{context.context_id}\n{_canonical_json(normalized)}".encode("utf-8")
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
        raise CognitiveBrainSchemaError(
            str(exc), code=_schema_failure_code(exc),
        ) from exc


def _nullable_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be null or a non-empty string")
    return value.strip()


def _schema_failure_code(exc: BaseException) -> str:
    message = str(exc)
    if "mode is not available" in message:
        return "mode_unavailable"
    if "stale reference" in message or "stale or mismatched" in message:
        return "stale_reference"
    if "WAIT" in message:
        return "wait_invariant"
    if "SPEAK" in message:
        return "speak_invariant"
    if "reason_codes" in message:
        return "reason_code"
    if "exceeds Brain bound" in message or "configured bound" in message:
        return "bound"
    if "trimmed" in message or "non-empty string" in message:
        return "text_shape"
    return "contract"


def _strict_string_tuple(
    value: Any, name: str, *, deduplicate: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must contain only strings")
    return tuple(dict.fromkeys(value)) if deduplicate else tuple(value)


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
