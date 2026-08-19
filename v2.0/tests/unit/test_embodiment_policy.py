from __future__ import annotations

from datetime import datetime, timezone

from interfaces.animation import MoodState
from interfaces.compatibility import ActionRequest, ActionStatus
from services.action.legacy_adapters import (
    AvatarGestureAuthority,
    AvatarGestureExecutor,
    AvatarGestureVerifier,
)
from services.animation.embodiment_policy import EmbodimentPolicy, EmbodimentPolicyConfig


class Animation:
    def __init__(self, *, acknowledged: bool = True) -> None:
        self.acknowledged = acknowledged
        self.expressions: list[object] = []
        self.gestures: list[str] = []

    async def express(self, command: object) -> None:
        self.expressions.append(command)

    async def trigger_intentional_gesture(self, gesture_id: str) -> bool:
        self.gestures.append(gesture_id)
        return self.acknowledged


def _policy(animation: Animation, *, now: list[float] | None = None) -> EmbodimentPolicy:
    clock = (lambda: now[0]) if now is not None else None
    return EmbodimentPolicy(
        EmbodimentPolicyConfig(2.0, 3.0, 2, 8), animation=animation, enabled=True, clock=clock,
    )


def _request(action_id: str, *, evidence: tuple[str, ...] = ("event-1",)) -> ActionRequest:
    return ActionRequest(
        schema_version=1, action_id=action_id, capability_id="AVATAR_GESTURE",
        action_type="AVATAR_GESTURE", target=None, arguments={"gesture_id": "wave"},
        intention_id=None, evidence_refs=evidence, idempotency_key=action_id,
        priority=0.0, requested_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        transaction_policy="none",
    )


async def test_mid_policy_is_cooldown_bound_and_never_changes_mood_input() -> None:
    now = [10.0]
    animation = Animation()
    policy = _policy(animation, now=now)
    await policy.start()
    mood = MoodState(vui=5)
    assert await policy.apply_mid("delivery-1", mood) is True
    assert await policy.apply_mid("delivery-2", mood) is False
    assert mood == MoodState(vui=5)
    assert len(animation.expressions) == 1
    assert policy.snapshot()["counts"]["mid_skipped_cooldown"] == 1


async def test_high_policy_rejects_overlap_and_releases_only_verified_acknowledgement() -> None:
    now = [10.0]
    policy = _policy(Animation(), now=now)
    await policy.start()
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is True
    assert await policy.begin_intentional("action-2", "wave", ("event-2",)) is False
    await policy.finish_intentional("action-1", True)
    snapshot = policy.snapshot()
    assert snapshot["active_high"] is None
    assert snapshot["counts"]["high_verified"] == 1
    assert snapshot["recent"][0]["evidence_refs"] == ("event-1",)


async def test_avatar_action_requires_grounded_evidence_and_records_vts_outcome() -> None:
    animation = Animation(acknowledged=True)
    policy = _policy(animation)
    await policy.start()
    authority = AvatarGestureAuthority(8)
    executor = AvatarGestureExecutor(
        animation, authority, enabled=True, policy=policy,
    )
    verifier = AvatarGestureVerifier(authority, enabled=True)
    await executor.start()
    await verifier.start()
    result = await executor.execute(_request("action-1", evidence=("event-1", "event-2", "event-3")))
    assert result.status is ActionStatus.SUCCESS
    assert result.result_data["evidence_refs"] == ("event-1", "event-2", "event-3")
    assert (await verifier.verify(_request("action-1"), result)).verified is True
    missing = await executor.execute(_request("action-2", evidence=()))
    assert missing.status is ActionStatus.REJECTED
    assert missing.error_code == "embodiment_policy_rejected"


async def test_vts_rejection_is_failed_not_verified_and_releases_high_lease() -> None:
    animation = Animation(acknowledged=False)
    policy = _policy(animation)
    await policy.start()
    executor = AvatarGestureExecutor(
        animation, AvatarGestureAuthority(8), enabled=True, policy=policy,
    )
    await executor.start()
    result = await executor.execute(_request("action-1"))
    assert result.status is ActionStatus.FAILED
    assert policy.snapshot()["active_high"] is None
    assert policy.snapshot()["counts"]["high_failed"] == 1


async def test_policy_metrics_failure_does_not_leak_intentional_lease() -> None:
    class BrokenMetrics:
        def record_embodiment_policy(self, *_args: object) -> None:
            raise RuntimeError("metrics unavailable")

    policy = EmbodimentPolicy(
        EmbodimentPolicyConfig(2.0, 3.0, 2, 8),
        animation=Animation(),
        metrics=BrokenMetrics(),
        enabled=True,
    )
    await policy.start()
    assert await policy.begin_intentional("action-1", "wave", ("event-1",)) is True
    await policy.finish_intentional("action-1", False)
    assert policy.snapshot()["active_high"] is None
