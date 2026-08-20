"""Strict contracts for the local speech and avatar action boundary."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from interfaces.animation import (
    AnimationService,
    EmbodimentPolicyService,
    EmbodimentSnapshot,
    IntentionalGestureOutcome,
)
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.tts import AudioChunk, TTSDeliveryMode, TTSDeliveryResult
from services.action.legacy_adapters import (
    ActionAdapterConfig,
    AvatarGestureAuthority,
    AvatarGestureExecutor,
    AvatarGestureVerifier,
    LocalActionAdapterBoundary,
    SpeechDeliveryAuthority,
    SpeechDeliveryExecutor,
    SpeechDeliveryVerifier,
)
from services.animation.vts_service import VTSAnimationService


def _request(action_type: str, **arguments: object) -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id=f"action:{action_type}",
        capability_id=action_type,
        action_type=action_type,
        target=None,
        arguments=arguments,
        intention_id=None,
        evidence_refs=(f"evidence:{action_type}",),
        idempotency_key=f"key:{action_type}",
        priority=0.0,
        requested_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        transaction_policy="none" if action_type == "AVATAR_GESTURE" else "delivery_aware",
    )


class _Animation(AnimationService):
    service_id = "test_animation"

    def __init__(self, acknowledgement: object) -> None:
        self.enabled = True
        self.running = True
        self.acknowledgement = acknowledgement
        self.calls: list[str] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {}

    async def express(self, command: object) -> None:
        return

    async def trigger_intentional_gesture(self, gesture_id: str) -> object:
        self.calls.append(gesture_id)
        return self.acknowledgement

    def is_intentional_gesture_allowed(self, gesture_id: str) -> bool:
        return gesture_id == "wave"

    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        return


class _Policy(EmbodimentPolicyService):
    service_id = "test_embodiment_policy"
    enabled = True

    def __init__(self) -> None:
        self.active: str | None = None
        self.finished: list[
            tuple[str, IntentionalGestureOutcome, str | None]
        ] = []

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        self.active = None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {}

    async def apply_mid(self, delivery_id: str, mood: object) -> bool:
        return True

    async def begin_intentional(
        self, action_id: str, gesture_id: str, evidence_refs: tuple[str, ...],
    ) -> bool:
        if self.active is not None or gesture_id != "wave" or not evidence_refs:
            return False
        self.active = action_id
        return True

    async def finish_intentional(
        self,
        action_id: str,
        outcome: IntentionalGestureOutcome,
        verification_source: str | None = None,
    ) -> bool:
        if self.active != action_id:
            return False
        self.active = None
        self.finished.append((action_id, outcome, verification_source))
        return True

    def snapshot(self) -> EmbodimentSnapshot:
        return EmbodimentSnapshot(
            running=True,
            enabled=True,
            active_level=None,
            active_action_id=None,
            active_gesture_id=None,
            counts={},
            recent=(),
        )


def _local_boundary(
    speak: Any,
    *,
    animation: Any = None,
    speech_enabled: bool = True,
    avatar_enabled: bool = True,
    max_records: int = 4,
    metrics: Any = None,
    policy: Any = None,
) -> LocalActionAdapterBoundary:
    authority = SpeechDeliveryAuthority(max_records)
    avatar_authority = AvatarGestureAuthority(max_records)
    active_policy = policy if policy is not None else _Policy()
    return LocalActionAdapterBoundary(
        ActionAdapterConfig(1.0, max_records, 4),
        speech_executor=SpeechDeliveryExecutor(
            speak, authority, enabled=speech_enabled, metrics=metrics,
        ),
        speech_verifier=SpeechDeliveryVerifier(
            authority, enabled=speech_enabled, metrics=metrics,
        ),
        avatar_executor=AvatarGestureExecutor(
            animation or _Animation(False), avatar_authority, enabled=avatar_enabled,
            metrics=metrics, policy=active_policy,
        ),
        avatar_verifier=AvatarGestureVerifier(
            avatar_authority, enabled=avatar_enabled, metrics=metrics,
            policy=active_policy,
        ),
        metrics=metrics,
    )


@pytest.mark.parametrize(
    "values",
    [
        (True, 4, 4),
        (0.0, 4, 4),
        (float("nan"), 4, 4),
        (1.0, True, 4),
        (1.0, 4, 0),
    ],
)
def test_action_adapter_config_is_strict(values: tuple[object, object, object]) -> None:
    with pytest.raises(ValueError):
        ActionAdapterConfig(*values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_speech_boundary_accepts_typed_subtitle_once_and_deduplicates() -> None:
    calls: list[tuple[str, str]] = []

    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        calls.append((request_id, text))
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    boundary = _local_boundary(speak)
    await boundary.start()
    request = _request("SPEAK", text="xin chào")
    first = await boundary.execute(request)
    duplicate = await boundary.execute(request)

    assert first is duplicate
    assert first.status is ActionStatus.SUCCESS
    assert first.verified is True
    assert first.verification_source == "tts_delivery"
    assert calls == [("action:SPEAK", "xin chào")]
    assert boundary.snapshot()["idempotency_records"] == 1


@pytest.mark.asyncio
async def test_speech_boundary_accepts_complete_mixed_delivery() -> None:
    async def speak(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.MIXED,
            sentences_total=2,
            sentences_delivered=2,
            audio_sentences=1,
            subtitle_sentences=1,
        )

    boundary = _local_boundary(speak)
    await boundary.start()
    result = await boundary.execute(_request("SELF_TALK", text="một. hai."))
    assert result.verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery", "error_code"),
    [
        (
            TTSDeliveryResult(
                request_id="action:SPEAK",
                delivered=False,
                mode=TTSDeliveryMode.MIXED,
                sentences_total=2,
                sentences_delivered=1,
                subtitle_sentences=1,
                failed_sentences=1,
            ),
            "delivery_not_confirmed",
        ),
        (
            TTSDeliveryResult(
                request_id="wrong",
                delivered=True,
                mode=TTSDeliveryMode.SUBTITLE,
                sentences_total=1,
                sentences_delivered=1,
                subtitle_sentences=1,
            ),
            "delivery_request_mismatch",
        ),
        (
            TTSDeliveryResult(
                request_id="action:SPEAK",
                delivered=True,
                mode=TTSDeliveryMode.NONE,
                sentences_total=1,
                sentences_delivered=1,
                subtitle_sentences=1,
            ),
            "delivery_contract_invalid",
        ),
    ],
)
async def test_speech_boundary_rejects_non_authoritative_delivery(
    delivery: TTSDeliveryResult, error_code: str,
) -> None:
    async def speak(_request_id: str, _text: str) -> TTSDeliveryResult:
        return delivery

    boundary = _local_boundary(speak)
    await boundary.start()
    result = await boundary.execute(_request("SPEAK", text="một. hai."))
    assert result.verified is False
    assert result.error_code == error_code


@pytest.mark.asyncio
async def test_speech_boundary_rejects_missing_and_untyped_callbacks() -> None:
    missing = _local_boundary(None)
    await missing.start()
    assert (
        await missing.execute(_request("SPEAK", text="xin chào"))
    ).error_code == "delivery_callback_missing"

    async def untyped(_request_id: str, _text: str) -> object:
        return object()

    invalid = _local_boundary(untyped)
    await invalid.start()
    assert (
        await invalid.execute(_request("SPEAK", text="xin chào"))
    ).error_code == "delivery_result_untyped"


@pytest.mark.asyncio
async def test_speech_cancellation_propagates_without_cached_success() -> None:
    async def cancel(_request_id: str, _text: str) -> TTSDeliveryResult:
        raise asyncio.CancelledError

    boundary = _local_boundary(cancel)
    await boundary.start()
    with pytest.raises(asyncio.CancelledError):
        await boundary.execute(_request("FOLLOW_UP", text="tiếp tục"))
    assert boundary.snapshot()["idempotency_records"] == 0


@pytest.mark.asyncio
async def test_idempotency_conflict_does_not_call_speech_twice() -> None:
    calls = 0

    async def speak(request_id: str, _text: str) -> TTSDeliveryResult:
        nonlocal calls
        calls += 1
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.AUDIO,
            sentences_total=1,
            sentences_delivered=1,
            audio_sentences=1,
        )

    boundary = _local_boundary(speak)
    await boundary.start()
    await boundary.execute(_request("SPEAK", text="một"))
    conflict = await boundary.execute(_request("SPEAK", text="khác"))
    assert conflict.error_code == "idempotency_conflict"
    assert calls == 1


@pytest.mark.asyncio
async def test_avatar_requires_strict_ack_and_deduplicates() -> None:
    animation = _Animation(True)
    boundary = _local_boundary(None, animation=animation)
    await boundary.start()
    request = _request("AVATAR_GESTURE", gesture_id="wave")
    first = await boundary.execute(request)
    duplicate = await boundary.execute(request)
    assert first is duplicate
    assert first.verified is True
    assert animation.calls == ["wave"]

    untyped_animation = _Animation(1)
    untyped_boundary = _local_boundary(None, animation=untyped_animation)
    await untyped_boundary.start()
    assert (
        await untyped_boundary.execute(request)
    ).error_code == "vts_not_acknowledged"


@pytest.mark.asyncio
async def test_avatar_adapter_fails_closed_when_embodiment_policy_is_disabled() -> None:
    animation = _Animation(True)
    policy = _Policy()
    policy.enabled = False
    boundary = _local_boundary(None, animation=animation, policy=policy)
    await boundary.start()
    result = await boundary.execute(_request("AVATAR_GESTURE", gesture_id="wave"))
    assert result.error_code == "embodiment_policy_unavailable"
    assert animation.calls == []


@pytest.mark.asyncio
async def test_avatar_verifier_rejects_forged_result_without_vts_authority() -> None:
    authority = AvatarGestureAuthority(4)
    verifier = AvatarGestureVerifier(authority, enabled=True)
    await verifier.start()
    request = _request("AVATAR_GESTURE", gesture_id="wave")
    now = datetime.now(timezone.utc)
    forged = ActionResult(
        schema_version=1,
        action_id=request.action_id,
        status=ActionStatus.SUCCESS,
        started_at=now,
        completed_at=now,
        verified=False,
        verification_source=None,
        result_data={"gesture_id": "wave", "vts_acknowledged": True},
    )
    assert (await verifier.verify(request, forged)).verified is False


@pytest.mark.asyncio
async def test_avatar_policy_lease_finishes_on_failure() -> None:
    policy = _Policy()
    boundary = _local_boundary(
        None, animation=_Animation(False), policy=policy,
    )
    await boundary.start()
    result = await boundary.execute(_request("AVATAR_GESTURE", gesture_id="wave"))
    assert result.error_code == "vts_not_acknowledged"
    assert policy.finished == [(
        "action:AVATAR_GESTURE", IntentionalGestureOutcome.FAILED, None,
    )]


class _BrokenMetrics:
    def record_action_adapter(self, *_args: object) -> None:
        raise RuntimeError("metrics unavailable")


@pytest.mark.asyncio
async def test_lifecycle_toggle_bounds_and_metrics_failure_are_isolated() -> None:
    async def speak(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    boundary = _local_boundary(speak, max_records=2, metrics=_BrokenMetrics())
    stopped = await boundary.execute(_request("SPEAK", text="stopped"))
    assert stopped.error_code == "boundary_stopped"
    await boundary.start()
    boundary.set_speech_enabled(False)
    assert boundary.speech_enabled is False
    boundary.set_speech_enabled(True)
    for index in range(3):
        request = replace(
            _request("SPEAK", text=str(index)),
            action_id=f"speech-{index}",
            idempotency_key=f"speech-{index}",
        )
        result = await boundary.execute(request)
        assert result.verified is True
    assert boundary.snapshot()["idempotency_records"] == 2
    await boundary.stop()
    assert (await boundary.health_check()).state.value == "stopped"


class _Transport:
    connected = True
    hotkeys = ("Wave",)

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def trigger(self, hotkey: str) -> bool:
        return hotkey == "Wave"


class _UntypedTransport(_Transport):
    async def trigger(self, hotkey: str) -> object:
        return 1


@pytest.mark.asyncio
async def test_vts_intentional_gesture_is_allowlisted_and_fail_safe() -> None:
    service = VTSAnimationService(
        _Transport(), mood_hotkeys={}, intentional_gesture_hotkeys={"wave": "Wave"},
    )
    await service.start()
    assert service.is_intentional_gesture_allowed("wave") is True
    assert service.is_intentional_gesture_allowed(" wave ") is False
    assert await service.trigger_intentional_gesture("wave") is True
    assert await service.trigger_intentional_gesture("unknown") is False
    await service.stop()
    assert await service.trigger_intentional_gesture("wave") is False

    untyped = VTSAnimationService(
        _UntypedTransport(), mood_hotkeys={},
        intentional_gesture_hotkeys={"wave": "Wave"},
    )
    await untyped.start()
    assert await untyped.trigger_intentional_gesture("wave") is False
    assert untyped.get_metrics()["animation_errors_total"] == 1

    with pytest.raises(ValueError, match="allowlist entries"):
        VTSAnimationService(
            _Transport(), mood_hotkeys={},
            intentional_gesture_hotkeys={" wave ": "Wave"},
        )
