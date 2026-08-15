"""Typed adapters around the existing speech and VTube Studio boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from interfaces.action_execution import ActionExecutor, ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus


SpeakFn = Callable[[str, str], Awaitable[Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(
    request: ActionRequest,
    *,
    started_at: datetime,
    status: ActionStatus,
    error_code: str | None = None,
    data: dict[str, Any] | None = None,
) -> ActionResult:
    return ActionResult(
        schema_version=1,
        action_id=request.action_id,
        status=status,
        started_at=started_at,
        completed_at=_now(),
        verified=False,
        verification_source=None,
        result_data=data or {},
        error_code=error_code,
    )


class _ToggleableActionService:
    """Shared lifecycle and observability for non-owning action adapters."""

    enabled: bool
    service_id: str

    def __init__(self, *, enabled: bool, metrics: Any = None) -> None:
        self.enabled = bool(enabled)
        self._metrics = metrics
        self._running = False
        self._outcomes: dict[str, int] = {}

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

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
        return dict(sorted(self._outcomes.items()))

    def _record(self, outcome: str) -> None:
        key = str(outcome)
        self._outcomes[key] = self._outcomes.get(key, 0) + 1
        callback = getattr(self._metrics, "record_action_adapter", None)
        if callable(callback):
            callback(self.service_id, key)


class SpeechDeliveryExecutor(_ToggleableActionService, ActionExecutor):
    """Call the existing typed speech callback exactly once; never commits state."""

    service_id = "speech_delivery"

    def __init__(self, speak: SpeakFn | None, *, enabled: bool = False, metrics: Any = None) -> None:
        super().__init__(enabled=enabled, metrics=metrics)
        self._speak = speak

    async def execute(self, request: ActionRequest) -> ActionResult:
        started_at = _now()
        if not self.enabled:
            self._record("disabled")
            return _result(request, started_at=started_at, status=ActionStatus.REJECTED, error_code="adapter_disabled")
        text = request.arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            self._record("invalid_request")
            return _result(request, started_at=started_at, status=ActionStatus.REJECTED, error_code="speech_text_missing")
        if self._speak is None:
            self._record("callback_missing")
            return _result(request, started_at=started_at, status=ActionStatus.FAILED, error_code="delivery_callback_missing")
        try:
            delivery = await self._speak(request.action_id, text)
        except Exception:
            self._record("delivery_exception")
            return _result(request, started_at=started_at, status=ActionStatus.FAILED, error_code="delivery_exception")
        confirmed = bool(getattr(delivery, "delivered", False))
        total = int(getattr(delivery, "sentences_total", 0) or 0)
        delivered = int(getattr(delivery, "sentences_delivered", 0) or 0)
        data = {
            "request_id": str(getattr(delivery, "request_id", "")),
            "delivery_confirmed": confirmed,
            "sentences_total": total,
            "sentences_delivered": delivered,
            "mode": str(getattr(getattr(delivery, "mode", None), "value", "none")),
            "cancelled": bool(getattr(delivery, "cancelled", False)),
        }
        if confirmed and total > 0 and delivered == total:
            self._record("executed")
            return _result(request, started_at=started_at, status=ActionStatus.SUCCESS, data=data)
        self._record("delivery_unconfirmed")
        return _result(request, started_at=started_at, status=ActionStatus.FAILED, error_code="delivery_not_confirmed", data=data)


class SpeechDeliveryVerifier(_ToggleableActionService, ActionVerifier):
    """Treat the existing all-sentences TTS result as the speech authority."""

    service_id = "speech_delivery_verifier"

    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
        data = result.result_data
        verified = bool(
            self.enabled
            and result.action_id == request.action_id
            and result.status is ActionStatus.SUCCESS
            and data.get("delivery_confirmed") is True
            and int(data.get("sentences_total", 0)) > 0
            and int(data.get("sentences_total", 0)) == int(data.get("sentences_delivered", -1))
            and not bool(data.get("cancelled", False))
        )
        self._record("verified" if verified else "unverified")
        return VerificationResult(
            verified=verified,
            source="tts_delivery" if verified else None,
            reason_code="delivery_verified" if verified else "delivery_not_verified",
            evidence_refs=(str(data.get("request_id", "")),) if verified and data.get("request_id") else (),
        )


class AvatarGestureExecutor(_ToggleableActionService, ActionExecutor):
    """Request one intentional VTS gesture; automatic mood expression is excluded."""

    service_id = "avatar_adapter"

    def __init__(self, animation: Any, *, enabled: bool = False, metrics: Any = None) -> None:
        super().__init__(enabled=enabled, metrics=metrics)
        self._animation = animation

    async def execute(self, request: ActionRequest) -> ActionResult:
        started_at = _now()
        gesture_id = request.arguments.get("gesture_id")
        if not self.enabled:
            self._record("disabled")
            return _result(request, started_at=started_at, status=ActionStatus.REJECTED, error_code="adapter_disabled")
        if not isinstance(gesture_id, str) or not gesture_id.strip():
            self._record("invalid_request")
            return _result(request, started_at=started_at, status=ActionStatus.REJECTED, error_code="gesture_id_missing")
        trigger = getattr(self._animation, "trigger_intentional_gesture", None)
        if not callable(trigger):
            self._record("adapter_missing")
            return _result(request, started_at=started_at, status=ActionStatus.FAILED, error_code="avatar_adapter_missing")
        acknowledged = bool(await trigger(gesture_id))
        data = {"gesture_id": gesture_id, "vts_acknowledged": acknowledged}
        self._record("executed" if acknowledged else "rejected")
        return _result(
            request,
            started_at=started_at,
            status=ActionStatus.SUCCESS if acknowledged else ActionStatus.FAILED,
            error_code=None if acknowledged else "vts_not_acknowledged",
            data=data,
        )


class AvatarGestureVerifier(_ToggleableActionService, ActionVerifier):
    """Verify only the authoritative VTS API acknowledgement, never visual playback."""

    service_id = "avatar_state"

    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
        verified = bool(
            self.enabled
            and result.action_id == request.action_id
            and result.status is ActionStatus.SUCCESS
            and result.result_data.get("vts_acknowledged") is True
        )
        self._record("verified" if verified else "unverified")
        return VerificationResult(
            verified=verified,
            source="vts_api_ack" if verified else None,
            reason_code="vts_acknowledged" if verified else "vts_not_acknowledged",
            evidence_refs=(str(result.result_data.get("gesture_id")),) if verified else (),
        )
