from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from interfaces.animation import (
    AnimationService,
    EmbodimentLevel,
    IntentionalGestureOutcome,
    MoodState,
)
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionStatus
from interfaces.tts import AudioChunk
from services.execution.local import (
    ActionAdapterConfig,
    AvatarGestureAuthority,
    AvatarGestureExecutor,
    AvatarGestureVerifier,
    LocalActionAdapterBoundary,
    SpeechDeliveryAuthority,
    SpeechDeliveryExecutor,
    SpeechDeliveryVerifier,
)
from services.animation.embodiment_policy import EmbodimentPolicy, EmbodimentPolicyConfig


class Animation(AnimationService):
    service_id = "test_animation"

    def __init__(self, *, acknowledged: object = True) -> None:
        self.enabled = True
        self.running = True
        self.connected = True
        self.acknowledged = acknowledged
        self.expressions: list[object] = []
        self.gestures: list[str] = []
        self.gesture_started = asyncio.Event()
        self.gesture_release: asyncio.Event | None = None
        self.expression_started = asyncio.Event()
        self.expression_release: asyncio.Event | None = None
        self.expression_error: Exception | None = None

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        if not self.running:
            return HealthStatus.stopped(self.service_id)
        if not self.connected:
            return HealthStatus.degraded(self.service_id, "disconnected")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {"animation_connected": self.connected}

    async def express(self, command: object) -> None:
        self.expressions.append(command)
        self.expression_started.set()
        if self.expression_error is not None:
            raise self.expression_error
        if self.expression_release is not None:
            await self.expression_release.wait()

    async def trigger_intentional_gesture(self, gesture_id: str) -> bool:
        self.gestures.append(gesture_id)
        self.gesture_started.set()
        if self.gesture_release is not None:
            await self.gesture_release.wait()
        return self.acknowledged if isinstance(self.acknowledged, bool) else False

    def is_intentional_gesture_allowed(self, gesture_id: str) -> bool:
        return gesture_id == "wave"

    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        return


def _config(**overrides: object) -> EmbodimentPolicyConfig:
    values: dict[str, object] = {
        "mid_cooldown_s": 2.0,
        "mid_timeout_s": 0.05,
        "intentional_cooldown_s": 3.0,
        "intentional_lease_ttl_s": 5.0,
        "max_evidence_refs": 2,
        "max_recent_records": 8,
        "max_id_chars": 64,
        "max_gesture_id_chars": 16,
    }
    values.update(overrides)
    return EmbodimentPolicyConfig(**values)  # type: ignore[arg-type]


def _policy(
    animation: Animation, *, now: list[float] | None = None, metrics: Any = None,
) -> EmbodimentPolicy:
    clock = (lambda: now[0]) if now is not None else None
    return EmbodimentPolicy(
        _config(), animation=animation, enabled=True, clock=clock, metrics=metrics,
    )


def _request(action_id: str, *, evidence: tuple[str, ...] = ("event-1",)) -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id=action_id,
        capability_id="AVATAR_GESTURE",
        action_type="AVATAR_GESTURE",
        target=None,
        arguments={"gesture_id": "wave"},
        intention_id=None,
        evidence_refs=evidence,
        idempotency_key=action_id,
        priority=0.0,
        requested_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        transaction_policy="none",
    )


class Loader:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self, *_args: object) -> object:
        return self.value


@pytest.mark.parametrize(
    "override",
    [
        {"mid_cooldown_s": True},
        {"mid_timeout_s": 0.0},
        {"intentional_cooldown_s": "3"},
        {"intentional_lease_ttl_s": 0.0},
        {"mid_cooldown_s": float("nan")},
        {"max_recent_records": True},
        {"max_id_chars": 0},
    ],
)
def test_config_rejects_coercion_and_invalid_bounds(override: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**override)


def test_loader_requires_exact_config_keys() -> None:
    raw = {
        "mid_cooldown_s": 2.0,
        "mid_timeout_s": 0.05,
        "intentional_cooldown_s": 3.0,
        "intentional_lease_ttl_s": 5.0,
        "max_evidence_refs": 2,
        "max_recent_records": 8,
        "max_id_chars": 64,
        "max_gesture_id_chars": 16,
    }
    assert EmbodimentPolicyConfig.from_loader(Loader(raw)) == _config()
    with pytest.raises(ValueError, match="keys invalid"):
        EmbodimentPolicyConfig.from_loader(Loader({**raw, "extra": 1}))
    missing = dict(raw)
    missing.pop("max_id_chars")
    with pytest.raises(ValueError, match="keys invalid"):
        EmbodimentPolicyConfig.from_loader(Loader(missing))


async def test_mid_is_post_delivery_cosmetic_cooldown_bound() -> None:
    now = [10.0]
    animation = Animation()
    policy = _policy(animation, now=now)
    await policy.start()
    mood = MoodState(vui=5)
    assert await policy.apply_mid("delivery-1", mood) is True
    assert await policy.apply_mid("delivery-2", mood) is False
    assert mood == MoodState(vui=5)
    assert len(animation.expressions) == 1
    snapshot = policy.snapshot()
    assert snapshot.counts["mid_dispatched"] == 1
    assert snapshot.counts["mid_skipped_cooldown"] == 1


async def test_mid_cancel_and_failure_cleanup_without_blocking_high() -> None:
    animation = Animation()
    animation.expression_release = asyncio.Event()
    policy = _policy(animation)
    await policy.start()
    task = asyncio.create_task(policy.apply_mid("delivery-1", MoodState(vui=4)))
    await animation.expression_started.wait()
    assert policy.snapshot().active_level is EmbodimentLevel.MID
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert policy.snapshot().active_level is None
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is True
    await policy.finish_intentional("action-1", IntentionalGestureOutcome.FAILED)

    animation.expression_release = None
    animation.expression_error = RuntimeError("VTS failed")
    assert await policy.apply_mid("delivery-2", MoodState(vui=4)) is False
    assert policy.snapshot().counts["mid_failed"] == 1


async def test_disable_during_mid_prevents_late_completion_from_reopening_state() -> None:
    animation = Animation()
    animation.expression_release = asyncio.Event()
    policy = _policy(animation)
    await policy.start()
    task = asyncio.create_task(policy.apply_mid("delivery-1", MoodState(vui=4)))
    await animation.expression_started.wait()
    await policy.set_enabled(False)
    animation.expression_release.set()
    assert await task is False
    snapshot = policy.snapshot()
    assert snapshot.active_level is None
    assert snapshot.counts["mid_cancelled"] == 1
    assert "mid_dispatched" not in snapshot.counts


async def test_mid_timeout_is_bounded_and_releases_conflict_state() -> None:
    animation = Animation()
    animation.expression_release = asyncio.Event()
    policy = _policy(animation)
    await policy.start()
    assert await policy.apply_mid("delivery-timeout", MoodState(vui=4)) is False
    snapshot = policy.snapshot()
    assert snapshot.active_level is None
    assert snapshot.counts["mid_timeout"] == 1


async def test_degraded_animation_rejects_mid_and_high_fail_safe() -> None:
    animation = Animation()
    animation.connected = False
    policy = _policy(animation)
    await policy.start()
    assert await policy.apply_mid("delivery-1", MoodState(vui=2)) is False
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is False
    assert (await policy.health_check()).state.value == "degraded"
    snapshot = policy.snapshot()
    assert snapshot.counts["mid_skipped_unavailable"] == 1
    assert snapshot.counts["high_rejected_unavailable"] == 1


async def test_high_requires_allowlist_bounded_evidence_and_single_lease() -> None:
    policy = _policy(Animation())
    await policy.start()
    assert await policy.begin_intentional("action-1", "unknown", ("event-1",)) is False
    assert await policy.begin_intentional("action-1", "wave", ()) is False
    assert await policy.begin_intentional("action-1", "wave", ("e1", "e2", "e3")) is False
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is True
    assert await policy.begin_intentional("action-2", "wave", ("event-2",)) is False
    assert policy.snapshot().active_action_id == "action-1"


async def test_verified_record_exists_only_after_authoritative_verifier() -> None:
    animation = Animation(acknowledged=True)
    policy = _policy(animation)
    await policy.start()
    authority = AvatarGestureAuthority(8)
    executor = AvatarGestureExecutor(animation, authority, enabled=True, policy=policy)
    verifier = AvatarGestureVerifier(authority, enabled=True, policy=policy)
    await executor.start()
    await verifier.start()

    request = _request("action-1")
    result = await executor.execute(request)
    assert result.status is ActionStatus.SUCCESS
    assert policy.snapshot().active_action_id == "action-1"
    assert "high_verified" not in policy.snapshot().counts

    verification = await verifier.verify(request, result)
    assert verification.verified is True
    snapshot = policy.snapshot()
    assert snapshot.active_action_id is None
    assert snapshot.counts["high_verified"] == 1
    assert snapshot.recent[-1].verification_source == "vts_api_ack"


async def test_vts_rejection_releases_lease_without_verified_record() -> None:
    animation = Animation(acknowledged=False)
    policy = _policy(animation)
    await policy.start()
    executor = AvatarGestureExecutor(
        animation, AvatarGestureAuthority(8), enabled=True, policy=policy,
    )
    await executor.start()
    result = await executor.execute(_request("action-1"))
    assert result.status is ActionStatus.FAILED
    snapshot = policy.snapshot()
    assert snapshot.active_action_id is None
    assert snapshot.counts["high_failed"] == 1
    assert "high_verified" not in snapshot.counts


async def test_executor_timeout_cleans_high_lease_without_verified_record() -> None:
    animation = Animation()
    animation.gesture_release = asyncio.Event()
    policy = _policy(animation)
    await policy.start()
    authority = AvatarGestureAuthority(8)
    boundary = LocalActionAdapterBoundary(
        ActionAdapterConfig(0.01, 8, 2),
        speech_executor=SpeechDeliveryExecutor(None, SpeechDeliveryAuthority(8)),
        speech_verifier=SpeechDeliveryVerifier(SpeechDeliveryAuthority(8)),
        avatar_executor=AvatarGestureExecutor(
            animation, authority, enabled=True, policy=policy,
        ),
        avatar_verifier=AvatarGestureVerifier(
            authority, enabled=True, policy=policy,
        ),
    )
    await boundary.start()
    result = await boundary.execute(_request("action-timeout"))
    assert result.status is ActionStatus.TIMEOUT
    snapshot = policy.snapshot()
    assert snapshot.active_action_id is None
    assert snapshot.counts["high_cancelled"] == 1
    assert "high_verified" not in snapshot.counts


async def test_unverified_action_finishes_high_as_failed() -> None:
    animation = Animation()
    policy = _policy(animation)
    await policy.start()
    authority = AvatarGestureAuthority(8)
    executor = AvatarGestureExecutor(animation, authority, enabled=True, policy=policy)
    verifier = AvatarGestureVerifier(authority, enabled=False, policy=policy)
    await executor.start()
    await verifier.start()
    request = _request("action-unverified")
    result = await executor.execute(request)
    assert (await verifier.verify(request, result)).verified is False
    snapshot = policy.snapshot()
    assert snapshot.active_action_id is None
    assert snapshot.counts["high_failed"] == 1
    assert "high_verified" not in snapshot.counts


async def test_stale_lease_expires_and_disable_cleans_active_state() -> None:
    now = [10.0]
    policy = _policy(Animation(), now=now)
    await policy.start()
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is True
    now[0] = 15.0
    await policy.health_check()
    assert policy.snapshot().active_action_id is None
    assert policy.snapshot().counts["high_expired"] == 1
    now[0] = 19.0
    assert await policy.begin_intentional("action-2", "wave", ("event-2",)) is True
    await policy.set_enabled(False)
    assert policy.snapshot().active_action_id is None
    assert policy.snapshot().counts["high_cancelled"] == 1


async def test_snapshot_is_deeply_immutable_bounded_and_metrics_fail_safe() -> None:
    class BrokenMetrics:
        def record_embodiment_policy(self, *_args: object) -> None:
            raise RuntimeError("metrics unavailable")

    policy = EmbodimentPolicy(
        _config(max_recent_records=2),
        animation=Animation(),
        metrics=BrokenMetrics(),
        enabled=True,
    )
    await policy.start()
    for index in range(3):
        assert await policy.apply_mid(f"delivery-{index}", MoodState(vui=index + 1)) is (
            index == 0
        )
    snapshot = policy.snapshot()
    assert len(snapshot.recent) == 2
    with pytest.raises(TypeError):
        snapshot.counts["changed"] = 1  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.recent[-1].outcome = "changed"  # type: ignore[misc]
    assert policy.get_metrics()["embodiment_policy_recent_records"] == 2


async def test_same_action_evidence_replays_to_same_arbitration_records() -> None:
    async def replay() -> dict[str, object]:
        now = [10.0]
        policy = _policy(Animation(), now=now)
        await policy.start()
        assert await policy.apply_mid("delivery-1", MoodState(vui=3)) is True
        now[0] = 13.0
        assert await policy.begin_intentional(
            "action-1", "wave", ("event-1", "intention-1"),
        ) is True
        assert await policy.finish_intentional(
            "action-1", IntentionalGestureOutcome.VERIFIED, "vts_api_ack",
        ) is True
        return policy.snapshot().to_dict()

    assert await replay() == await replay()
