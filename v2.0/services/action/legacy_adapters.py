"""Strict adapters around existing speech and VTube Studio boundaries."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from interfaces.action_execution import (
    ActionExecutor,
    ActionVerifier,
    LocalActionBoundaryService,
    VerificationResult,
)
from interfaces.animation import EmbodimentPolicyService, IntentionalGestureOutcome
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult


SpeakFn = Callable[[str, str], Awaitable[TTSDeliveryResult]]
_SPEECH_ACTIONS = frozenset({"SPEAK", "SELF_TALK", "FOLLOW_UP"})
_LOCAL_ACTIONS = _SPEECH_ACTIONS | {"AVATAR_GESTURE"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(
    request: ActionRequest,
    *,
    started_at: datetime,
    status: ActionStatus,
    error_code: str | None = None,
    data: Mapping[str, Any] | None = None,
    verified: bool = False,
    verification_source: str | None = None,
) -> ActionResult:
    return ActionResult(
        schema_version=1,
        action_id=request.action_id,
        status=status,
        started_at=started_at,
        completed_at=_now(),
        verified=verified,
        verification_source=verification_source,
        result_data=data or {},
        error_code=error_code,
    )


def _strict_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive int")
    return value


@dataclass(frozen=True)
class ActionAdapterConfig:
    execution_timeout_s: float
    max_idempotency_records: int
    max_evidence_refs: int

    def __post_init__(self) -> None:
        timeout = self.execution_timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("execution_timeout_s must be a finite positive number")
        object.__setattr__(self, "execution_timeout_s", float(timeout))
        for name in ("max_idempotency_records", "max_evidence_refs"):
            object.__setattr__(
                self, name, _strict_positive_int(getattr(self, name), name),
            )

    @classmethod
    def from_loader(cls, loader: Any) -> "ActionAdapterConfig":
        raw = loader.get("capabilities", "action_adapters", {})
        if not isinstance(raw, Mapping):
            raise ValueError("action_adapters config must be a mapping")
        return cls(
            execution_timeout_s=raw.get("execution_timeout_s"),
            max_idempotency_records=raw.get("max_idempotency_records"),
            max_evidence_refs=raw.get("max_evidence_refs"),
        )


class _ToggleableActionService:
    """Strict lifecycle and fail-isolated observability for local adapters."""

    enabled: bool
    service_id: str

    def __init__(self, *, enabled: bool, metrics: Any = None) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self.enabled = enabled
        self._metrics = metrics
        self._running = False
        self._outcomes: dict[str, int] = {}

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self.enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self.enabled:
            return HealthStatus.degraded(self.service_id, "action adapter disabled")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {
            f"action_adapter_{self.service_id}_{key}_total": value
            for key, value in sorted(self._outcomes.items())
        }

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        callback = getattr(self._metrics, "record_action_adapter", None)
        if callable(callback):
            try:
                callback(self.service_id, outcome)
            except Exception:
                pass

    def _unavailable(self, request: ActionRequest, started_at: datetime) -> ActionResult | None:
        if not self._running:
            self._record("stopped")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="adapter_stopped",
            )
        if not self.enabled:
            self._record("disabled")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="adapter_disabled",
            )
        return None


class SpeechDeliveryAuthority:
    """Bounded store of the exact typed TTS outcomes read by the verifier."""

    def __init__(self, max_records: int) -> None:
        self._max_records = _strict_positive_int(max_records, "max_records")
        self._records: OrderedDict[str, TTSDeliveryResult] = OrderedDict()

    def put(self, action_id: str, delivery: TTSDeliveryResult) -> None:
        if not isinstance(delivery, TTSDeliveryResult):
            raise ValueError("delivery must be TTSDeliveryResult")
        self._records[action_id] = delivery
        self._records.move_to_end(action_id)
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)

    def get(self, action_id: str) -> TTSDeliveryResult | None:
        delivery = self._records.get(action_id)
        if delivery is not None:
            self._records.move_to_end(action_id)
        return delivery

    def __len__(self) -> int:
        return len(self._records)


def _speech_request_error(request: ActionRequest) -> str | None:
    if request.schema_version != 1:
        return "unsupported_schema"
    if request.action_type not in _SPEECH_ACTIONS:
        return "unsupported_action"
    if request.capability_id != request.action_type:
        return "capability_mismatch"
    if request.transaction_policy != "delivery_aware":
        return "transaction_policy_mismatch"
    if request.target is not None:
        return "invalid_target"
    if set(request.arguments) != {"text"}:
        return "invalid_arguments"
    text = request.arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        return "speech_text_missing"
    return None


def _delivery_error(request: ActionRequest, delivery: TTSDeliveryResult) -> str | None:
    if delivery.request_id != request.action_id:
        return "delivery_request_mismatch"
    counts = (
        delivery.sentences_total,
        delivery.sentences_delivered,
        delivery.audio_sentences,
        delivery.subtitle_sentences,
        delivery.failed_sentences,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
        return "delivery_contract_invalid"
    if delivery.delivered is not True or delivery.cancelled is not False:
        return "delivery_not_confirmed"
    if delivery.sentences_total <= 0:
        return "delivery_not_confirmed"
    if delivery.sentences_delivered != delivery.sentences_total:
        return "delivery_not_confirmed"
    if delivery.failed_sentences != 0:
        return "delivery_not_confirmed"
    if delivery.audio_sentences + delivery.subtitle_sentences != delivery.sentences_total:
        return "delivery_contract_invalid"
    if delivery.mode is TTSDeliveryMode.AUDIO:
        valid_mode = delivery.audio_sentences > 0 and delivery.subtitle_sentences == 0
    elif delivery.mode is TTSDeliveryMode.SUBTITLE:
        valid_mode = delivery.subtitle_sentences > 0 and delivery.audio_sentences == 0
    elif delivery.mode is TTSDeliveryMode.MIXED:
        valid_mode = delivery.audio_sentences > 0 and delivery.subtitle_sentences > 0
    else:
        valid_mode = False
    return None if valid_mode else "delivery_contract_invalid"


class SpeechDeliveryExecutor(_ToggleableActionService, ActionExecutor):
    """Call the existing TTS callback once and retain its exact typed outcome."""

    service_id = "speech_delivery"

    def __init__(
        self,
        speak: SpeakFn | None,
        authority: SpeechDeliveryAuthority,
        *,
        enabled: bool = False,
        metrics: Any = None,
    ) -> None:
        if not isinstance(authority, SpeechDeliveryAuthority):
            raise ValueError("authority must be SpeechDeliveryAuthority")
        super().__init__(enabled=enabled, metrics=metrics)
        self._speak = speak
        self._authority = authority

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        started_at = _now()
        unavailable = self._unavailable(request, started_at)
        if unavailable is not None:
            return unavailable
        request_error = _speech_request_error(request)
        if request_error is not None:
            self._record("invalid_request")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code=request_error,
            )
        if self._speak is None:
            self._record("callback_missing")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="delivery_callback_missing",
            )
        try:
            delivery = await self._speak(request.action_id, request.arguments["text"])
        except asyncio.CancelledError:
            self._record("cancelled")
            raise
        except Exception:
            self._record("delivery_exception")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="delivery_exception",
            )
        if not isinstance(delivery, TTSDeliveryResult):
            self._record("untyped_delivery")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="delivery_result_untyped",
            )
        self._authority.put(request.action_id, delivery)
        error = _delivery_error(request, delivery)
        data = {
            "request_id": delivery.request_id,
            "mode": delivery.mode.value,
            "sentences_total": delivery.sentences_total,
            "sentences_delivered": delivery.sentences_delivered,
            "cancelled": delivery.cancelled,
        }
        if error is not None:
            self._record(error)
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code=error, data=data,
            )
        self._record("executed")
        return _result(
            request, started_at=started_at, status=ActionStatus.SUCCESS, data=data,
        )


class SpeechDeliveryVerifier(_ToggleableActionService, ActionVerifier):
    """Verify against the exact typed TTS outcome, never executor result_data."""

    service_id = "speech_delivery_verifier"

    def __init__(
        self,
        authority: SpeechDeliveryAuthority,
        *,
        enabled: bool = False,
        metrics: Any = None,
    ) -> None:
        if not isinstance(authority, SpeechDeliveryAuthority):
            raise ValueError("authority must be SpeechDeliveryAuthority")
        super().__init__(enabled=enabled, metrics=metrics)
        self._authority = authority

    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
        if not isinstance(request, ActionRequest) or not isinstance(result, ActionResult):
            raise ValueError("request and result must be typed action contracts")
        delivery = self._authority.get(request.action_id)
        verified = (
            self._running
            and self.enabled
            and result.action_id == request.action_id
            and result.status is ActionStatus.SUCCESS
            and isinstance(delivery, TTSDeliveryResult)
            and _delivery_error(request, delivery) is None
        )
        self._record("verified" if verified else "unverified")
        return VerificationResult(
            verified=verified,
            source="tts_delivery",
            reason_code="delivery_verified" if verified else "delivery_not_verified",
            evidence_refs=(delivery.request_id,) if verified and delivery is not None else (),
        )


def _avatar_request_error(request: ActionRequest) -> str | None:
    if request.schema_version != 1:
        return "unsupported_schema"
    if request.action_type != "AVATAR_GESTURE" or request.capability_id != "AVATAR_GESTURE":
        return "capability_mismatch"
    if request.transaction_policy != "none":
        return "transaction_policy_mismatch"
    if request.target is not None or set(request.arguments) != {"gesture_id"}:
        return "invalid_arguments"
    gesture_id = request.arguments.get("gesture_id")
    if not isinstance(gesture_id, str) or not gesture_id or gesture_id != gesture_id.strip():
        return "gesture_id_invalid"
    return None


class AvatarGestureAuthority:
    """Bounded VTS acknowledgement records independent from ActionResult claims."""

    def __init__(self, max_records: int) -> None:
        self._max_records = _strict_positive_int(max_records, "max_records")
        self._records: OrderedDict[str, tuple[str, bool]] = OrderedDict()

    def put(self, action_id: str, gesture_id: str, acknowledged: bool) -> None:
        if not isinstance(acknowledged, bool):
            raise ValueError("acknowledged must be a bool")
        self._records[action_id] = (gesture_id, acknowledged)
        self._records.move_to_end(action_id)
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)

    def get(self, action_id: str) -> tuple[str, bool] | None:
        record = self._records.get(action_id)
        if record is not None:
            self._records.move_to_end(action_id)
        return record


class AvatarGestureExecutor(_ToggleableActionService, ActionExecutor):
    """Request one intentional VTS gesture; automatic mood expression is excluded."""

    service_id = "avatar_adapter"

    def __init__(
        self,
        animation: Any,
        authority: AvatarGestureAuthority,
        *,
        enabled: bool = False,
        metrics: Any = None,
        policy: Any = None,
    ) -> None:
        if not isinstance(authority, AvatarGestureAuthority):
            raise ValueError("authority must be AvatarGestureAuthority")
        super().__init__(enabled=enabled, metrics=metrics)
        self._animation = animation
        self._authority = authority
        self._policy = policy

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        started_at = _now()
        unavailable = self._unavailable(request, started_at)
        if unavailable is not None:
            return unavailable
        request_error = _avatar_request_error(request)
        if request_error is not None:
            self._record("invalid_request")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code=request_error,
            )
        gesture_id = request.arguments["gesture_id"]
        evidence_refs = request.evidence_refs
        if (
            not isinstance(self._policy, EmbodimentPolicyService)
            or getattr(self._policy, "enabled", None) is not True
        ):
            self._record("policy_unavailable")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="embodiment_policy_unavailable",
                data={"gesture_id": gesture_id, "evidence_refs": evidence_refs},
            )
        try:
            granted = await self._policy.begin_intentional(
                request.action_id, gesture_id, evidence_refs,
            )
        except asyncio.CancelledError:
            self._record("cancelled")
            raise
        except Exception:
            granted = False
        if granted is not True:
            self._record("policy_rejected")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="embodiment_policy_rejected",
                data={"gesture_id": gesture_id, "evidence_refs": evidence_refs},
            )
        acknowledged = False
        try:
            trigger = getattr(self._animation, "trigger_intentional_gesture", None)
            if not callable(trigger):
                self._record("adapter_missing")
                await self._finish_policy(
                    request.action_id, IntentionalGestureOutcome.FAILED,
                )
                return _result(
                    request, started_at=started_at, status=ActionStatus.FAILED,
                    error_code="avatar_adapter_missing",
                    data={"gesture_id": gesture_id, "evidence_refs": evidence_refs},
                )
            acknowledgement = await trigger(gesture_id)
            acknowledged = acknowledgement is True
            if isinstance(acknowledgement, bool):
                self._authority.put(request.action_id, gesture_id, acknowledgement)
            else:
                self._record("ack_untyped")
        except asyncio.CancelledError:
            self._record("cancelled")
            await self._finish_policy(
                request.action_id, IntentionalGestureOutcome.CANCELLED,
            )
            raise
        except Exception:
            self._record("trigger_exception")
        if not acknowledged:
            await self._finish_policy(
                request.action_id, IntentionalGestureOutcome.FAILED,
            )
        data = {
            "gesture_id": gesture_id,
            "vts_acknowledged": acknowledged,
            "evidence_refs": evidence_refs,
        }
        self._record("executed" if acknowledged else "not_acknowledged")
        return _result(
            request, started_at=started_at,
            status=ActionStatus.SUCCESS if acknowledged else ActionStatus.FAILED,
            error_code=None if acknowledged else "vts_not_acknowledged", data=data,
        )

    async def _finish_policy(
        self,
        action_id: str,
        outcome: IntentionalGestureOutcome,
        verification_source: str | None = None,
    ) -> bool:
        try:
            return await self._policy.finish_intentional(
                action_id, outcome, verification_source,
            ) is True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record("policy_finish_failed")
            return False


class AvatarGestureVerifier(_ToggleableActionService, ActionVerifier):
    """Verify only a strict VTS API acknowledgement, never visual playback."""

    service_id = "avatar_state"

    def __init__(
        self,
        authority: AvatarGestureAuthority,
        *,
        enabled: bool = False,
        metrics: Any = None,
        policy: Any = None,
    ) -> None:
        if not isinstance(authority, AvatarGestureAuthority):
            raise ValueError("authority must be AvatarGestureAuthority")
        super().__init__(enabled=enabled, metrics=metrics)
        self._authority = authority
        self._policy = policy

    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
        if not isinstance(request, ActionRequest) or not isinstance(result, ActionResult):
            raise ValueError("request and result must be typed action contracts")
        authority_record = self._authority.get(request.action_id)
        authority_verified = (
            self._running
            and self.enabled
            and _avatar_request_error(request) is None
            and result.action_id == request.action_id
            and result.status is ActionStatus.SUCCESS
            and authority_record == (request.arguments.get("gesture_id"), True)
        )
        verified = authority_verified
        policy_reason = "vts_not_acknowledged"
        if isinstance(self._policy, EmbodimentPolicyService):
            try:
                finished = await self._policy.finish_intentional(
                    request.action_id,
                    (
                        IntentionalGestureOutcome.VERIFIED
                        if verified else IntentionalGestureOutcome.FAILED
                    ),
                    "vts_api_ack" if verified else None,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                finished = False
            if finished is not True:
                verified = False
                policy_reason = "embodiment_policy_finalize_failed"
        else:
            verified = False
            policy_reason = "embodiment_policy_unavailable"
        self._record("verified" if verified else "unverified")
        return VerificationResult(
            verified=verified,
            source="vts_api_ack",
            reason_code="vts_acknowledged" if verified else policy_reason,
            evidence_refs=(request.arguments["gesture_id"],) if verified else (),
        )

    async def abort_intentional(
        self, action_id: str, outcome: IntentionalGestureOutcome,
    ) -> None:
        if not isinstance(self._policy, EmbodimentPolicyService):
            return
        try:
            await self._policy.finish_intentional(action_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record("policy_finish_failed")


@dataclass(frozen=True)
class _IdempotencyRecord:
    fingerprint: str
    result: ActionResult


class LocalActionAdapterBoundary(LocalActionBoundaryService):
    """Route and verify local side effects without committing business state."""

    service_id = "local_action_adapter_boundary"

    def __init__(
        self,
        config: ActionAdapterConfig,
        *,
        speech_executor: SpeechDeliveryExecutor,
        speech_verifier: SpeechDeliveryVerifier,
        avatar_executor: AvatarGestureExecutor,
        avatar_verifier: AvatarGestureVerifier,
        metrics: Any = None,
    ) -> None:
        if not isinstance(config, ActionAdapterConfig):
            raise ValueError("config must be ActionAdapterConfig")
        self._config = config
        self._speech_executor = speech_executor
        self._speech_verifier = speech_verifier
        self._avatar_executor = avatar_executor
        self._avatar_verifier = avatar_verifier
        self._metrics = metrics
        self._running = False
        self._lock = asyncio.Lock()
        self._idempotency: OrderedDict[str, _IdempotencyRecord] = OrderedDict()
        self._outcomes: dict[str, int] = {}

    @property
    def speech_enabled(self) -> bool:
        return self._speech_executor.enabled and self._speech_verifier.enabled

    @property
    def avatar_enabled(self) -> bool:
        return self._avatar_executor.enabled and self._avatar_verifier.enabled

    def set_speech_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._speech_executor.set_enabled(enabled)
        self._speech_verifier.set_enabled(enabled)

    def set_avatar_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._avatar_executor.set_enabled(enabled)
        self._avatar_verifier.set_enabled(enabled)

    async def start(self) -> None:
        if self._running:
            return
        for service in self._services():
            await service.start()
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        for service in reversed(self._services()):
            await service.stop()
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self.speech_enabled and not self.avatar_enabled:
            return HealthStatus.degraded(self.service_id, "local action adapters disabled")
        return HealthStatus.healthy(
            self.service_id,
            speech_enabled=self.speech_enabled,
            avatar_enabled=self.avatar_enabled,
        )

    def get_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "local_action_adapter_running": self._running,
            "local_action_adapter_idempotency_records": len(self._idempotency),
            **{
                f"local_action_adapter_{key}_total": value
                for key, value in sorted(self._outcomes.items())
            },
        }
        for service in self._services():
            metrics.update(service.get_metrics())
        return metrics

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "speech_enabled": self.speech_enabled,
            "avatar_enabled": self.avatar_enabled,
            "idempotency_records": len(self._idempotency),
            "outcomes": dict(sorted(self._outcomes.items())),
        }

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        fingerprint = _request_fingerprint(request)
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                self._idempotency.move_to_end(request.idempotency_key)
                if cached.fingerprint != fingerprint:
                    self._record("idempotency_conflict")
                    return _result(
                        request, started_at=_now(), status=ActionStatus.REJECTED,
                        error_code="idempotency_conflict",
                    )
                self._record("duplicate")
                return cached.result
            result = await self._execute_once(request)
            self._idempotency[request.idempotency_key] = _IdempotencyRecord(
                fingerprint, result,
            )
            while len(self._idempotency) > self._config.max_idempotency_records:
                self._idempotency.popitem(last=False)
            return result

    async def _execute_once(self, request: ActionRequest) -> ActionResult:
        started_at = _now()
        if not self._running:
            self._record("stopped")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="boundary_stopped",
            )
        if request.action_type not in _LOCAL_ACTIONS:
            self._record("unsupported_action")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="unsupported_action",
            )
        if len(request.evidence_refs) > self._config.max_evidence_refs:
            self._record("too_many_evidence_refs")
            return _result(
                request, started_at=started_at, status=ActionStatus.REJECTED,
                error_code="too_many_evidence_refs",
            )
        executor, verifier = self._route(request.action_type)
        try:
            result = await asyncio.wait_for(
                executor.execute(request), timeout=self._config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            self._record("cancelled")
            raise
        except asyncio.TimeoutError:
            self._record("execution_timeout")
            return _result(
                request, started_at=started_at, status=ActionStatus.TIMEOUT,
                error_code="execution_timeout",
            )
        except Exception:
            self._record("executor_exception")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="executor_exception",
            )
        if not isinstance(result, ActionResult):
            self._record("executor_failed")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="invalid_executor_result",
            )
        if result.action_id != request.action_id or result.verified:
            self._record("executor_failed")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="invalid_executor_result",
            )
        if result.status is not ActionStatus.SUCCESS:
            self._record("executor_failed")
            return result
        try:
            verification = await asyncio.wait_for(
                verifier.verify(request, result), timeout=self._config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            await self._abort_avatar_policy(
                request, IntentionalGestureOutcome.CANCELLED,
            )
            self._record("cancelled")
            raise
        except asyncio.TimeoutError:
            await self._abort_avatar_policy(
                request, IntentionalGestureOutcome.TIMEOUT,
            )
            self._record("verification_timeout")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="verification_timeout",
            )
        except Exception:
            await self._abort_avatar_policy(
                request, IntentionalGestureOutcome.FAILED,
            )
            self._record("verification_exception")
            return _result(
                request, started_at=started_at, status=ActionStatus.FAILED,
                error_code="verification_exception",
            )
        if not isinstance(verification, VerificationResult) or not verification.verified:
            self._record("unverified")
            reason = (
                verification.reason_code
                if isinstance(verification, VerificationResult)
                else "invalid_verification_result"
            )
            return _result(
                request, started_at=result.started_at, status=ActionStatus.FAILED,
                error_code=reason, data=result.result_data,
            )
        if len(verification.evidence_refs) > self._config.max_evidence_refs:
            self._record("verification_evidence_overflow")
            return _result(
                request, started_at=result.started_at, status=ActionStatus.FAILED,
                error_code="verification_evidence_overflow", data=result.result_data,
            )
        self._record("verified")
        return ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=ActionStatus.SUCCESS,
            started_at=result.started_at,
            completed_at=result.completed_at,
            verified=True,
            verification_source=verification.source,
            result_data=result.result_data,
            error_code=None,
        )

    def _route(self, action_type: str) -> tuple[ActionExecutor, ActionVerifier]:
        if action_type in _SPEECH_ACTIONS:
            return self._speech_executor, self._speech_verifier
        return self._avatar_executor, self._avatar_verifier

    async def _abort_avatar_policy(
        self, request: ActionRequest, outcome: IntentionalGestureOutcome,
    ) -> None:
        if request.action_type != "AVATAR_GESTURE":
            return
        await self._avatar_verifier.abort_intentional(request.action_id, outcome)

    def _services(self) -> tuple[_ToggleableActionService, ...]:
        return (
            self._speech_executor,
            self._speech_verifier,
            self._avatar_executor,
            self._avatar_verifier,
        )

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        callback = getattr(self._metrics, "record_action_adapter", None)
        if callable(callback):
            try:
                callback(self.service_id, outcome)
            except Exception:
                pass


def _request_fingerprint(request: ActionRequest) -> str:
    value = request.to_dict()
    value.pop("requested_at", None)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
