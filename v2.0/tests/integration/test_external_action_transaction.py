from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from interfaces.action_execution import ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, Capability
from interfaces.external_executor import (
    ExternalExecutorBinding,
    OBSCommandAck,
    OBSSceneState,
    OBSSceneTransportService,
)
from services.action.external_loop import ExternalActionConfig, ExternalActionLoop
from services.action.external_registry import ExternalExecutorRegistry
from services.action.obs_scene import OBSSceneConfig, OBSSceneExecutor, OBSSceneVerifier
from services.capability.registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRegistryConfig,
)
from services.director.action_transaction import ActionTransactionManager
from services.world.world_model import WorldModelConfig, WorldModelShadow


NOW = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


class FakeOBS(OBSSceneTransportService):
    service_id = "fake_obs"

    def __init__(self, scene: str = "Starting") -> None:
        self.scene = scene
        self.running = False
        self.get_calls = 0
        self.set_calls: list[str] = []
        self.override_set_scene: str | None = None
        self.fail_set_at: int | None = None
        self.block_set = False
        self.set_entered: asyncio.Event | None = None

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {"get_calls": self.get_calls, "set_calls": len(self.set_calls)}

    async def get_current_program_scene(self) -> OBSSceneState:
        self.get_calls += 1
        return OBSSceneState(self.scene, f"obs:get:{self.get_calls}")

    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck:
        self.set_calls.append(scene_name)
        if self.fail_set_at == len(self.set_calls):
            raise RuntimeError("sanitized fake failure")
        if self.block_set:
            assert self.set_entered is not None
            self.set_entered.set()
            await asyncio.Event().wait()
        self.scene = self.override_set_scene or scene_name
        return OBSCommandAck(
            request_id=f"set-{len(self.set_calls)}",
            accepted=True,
            evidence_ref=f"obs:set:{len(self.set_calls)}",
        )


def _obs_config() -> OBSSceneConfig:
    return OBSSceneConfig(
        host="127.0.0.1",
        port=4455,
        use_tls=False,
        password_env="OBS_WEBSOCKET_PASSWORD",
        connect_timeout_s=0.1,
        request_timeout_s=0.1,
        health_timeout_s=0.2,
        health_ttl_s=30.0,
        max_attempts=1,
        retry_backoff_s=0.0,
        max_scene_name_chars=64,
        max_authority_records=16,
        max_evidence_refs=4,
        max_message_bytes=4096,
    )


def _loop_config() -> ExternalActionConfig:
    return ExternalActionConfig(
        execution_timeout_s=0.2,
        verification_timeout_s=0.1,
        rollback_timeout_s=0.2,
        max_recent_results=4,
        max_idempotency_records=8,
        max_verification_evidence_refs=4,
        max_scene_name_chars=64,
        max_registry_bindings=2,
    )


def _request(*, key: str = "scene:main", scene: str = "Main") -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id=f"action:{key}",
        capability_id="SWITCH_SCENE",
        action_type="SWITCH_SCENE",
        target=scene,
        arguments={"scene_name": scene},
        intention_id=None,
        evidence_refs=("operator:test",),
        idempotency_key=key,
        priority=0.0,
        requested_at=NOW,
        transaction_policy="verified",
    )


def _world() -> WorldModelShadow:
    return WorldModelShadow(WorldModelConfig(
        allowed_domains=("stream",),
        default_ttl_s=60.0,
        source_authority={"runtime": 60},
        max_state_entries=8,
        max_evidence_refs=4,
        max_payload_items=8,
        max_payload_chars=256,
        dedup_ttl_s=60.0,
        max_dedup_keys=16,
    ), clock=lambda: NOW)


def _capabilities(
    world: Any,
    transactions: ActionTransactionManager,
    executor: OBSSceneExecutor,
    *,
    grant_permission: bool = True,
) -> CapabilityRegistry:
    wait = CapabilityDefinition(
        capability=Capability(
            capability_id="WAIT", action_type="WAIT", description="wait",
            executor_id="local_wait", verifier_id="local_wait", risk_level="low",
            required_permissions=(), parameter_schema={}, transaction_policy="none",
        ),
        health_target_id="local_wait", world_equals={}, self_equals={},
        conflict_actions=(), mock_only=False,
    )
    scene = CapabilityDefinition(
        capability=Capability(
            capability_id="SWITCH_SCENE", action_type="SWITCH_SCENE",
            description="verified OBS scene", executor_id="obs_scene",
            verifier_id="obs_scene_state", risk_level="medium",
            required_permissions=("scene.control",),
            parameter_schema={"scene_name": "string"}, transaction_policy="verified",
        ),
        health_target_id="obs_websocket",
        verifier_health_target_id="obs_websocket",
        world_equals={}, self_equals={}, conflict_actions=("SWITCH_SCENE",),
        mock_only=False,
    )
    registry = CapabilityRegistry(
        CapabilityRegistryConfig(
            max_evidence_refs=4,
            granted_permissions=frozenset({"scene.control"} if grant_permission else set()),
            definitions=(wait, scene),
        ),
        world_snapshot_provider=world.snapshot,
        transaction_snapshot_provider=transactions.snapshot,
        health_snapshot_provider=lambda: {"targets": {}},
        clock=lambda: NOW,
    )
    registry.register_verifier("obs_scene_state")
    registry.register_health_provider("obs_scene", executor.public_health)
    return registry


async def _build(
    transport: FakeOBS,
    *,
    enabled: bool = True,
    verifier: ActionVerifier | None = None,
    world: Any = None,
    grant_permission: bool = True,
) -> tuple[ExternalActionLoop, Any, ActionTransactionManager, OBSSceneExecutor]:
    actual_world = world or _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    executor = OBSSceneExecutor(_obs_config(), transport, enabled=enabled)
    actual_verifier = verifier or OBSSceneVerifier(
        _obs_config(), transport, enabled=enabled,
    )
    binding = ExternalExecutorBinding(
        "obs_scene", "obs_scene_state", "obs_scene_executor", "obs_websocket",
    )
    routes = ExternalExecutorRegistry(2, allowed_bindings=(binding,))
    routes.register(binding, executor, actual_verifier)
    capabilities = _capabilities(
        actual_world, transactions, executor, grant_permission=grant_permission,
    )
    loop = ExternalActionLoop(
        _loop_config(),
        capability_registry=capabilities,
        executor_registry=routes,
        transactions=transactions,
        world_model=actual_world,
        enabled=enabled,
        clock=lambda: NOW,
    )
    await capabilities.start()
    await routes.start()
    await loop.start()
    if enabled:
        assert (await executor.health_check()).is_ok
    return loop, actual_world, transactions, executor


def test_verified_scene_commits_before_world_projection_and_deduplicates() -> None:
    transport = FakeOBS()

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(transport)
        first = await loop.execute(_request())
        duplicate = await loop.execute(_request())
        assert first == duplicate
        assert first.verified is True
        assert first.verification_source == "obs_websocket"
        assert first.result_data["world_projected"] is True
        assert world.query("stream.current_scene").value == "Main"
        assert transactions.snapshot()["recent"][-1]["state"] == "committed"
        assert transport.set_calls == ["Main"]

    asyncio.run(scenario())


def test_ack_with_operator_scene_change_releases_and_skips_rollback() -> None:
    transport = FakeOBS()
    transport.override_set_scene = "OperatorScene"

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(transport)
        result = await loop.execute(_request(key="operator-race"))
        assert result.verified is False
        assert result.error_code == "scene_mismatch"
        assert result.result_data["rollback_status"] == "skipped"
        assert result.result_data["rollback_reason"] == "rollback_operator_scene_changed"
        assert transport.scene == "OperatorScene"
        assert transport.set_calls == ["Main"]
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_verification_failure_restores_previous_scene_and_never_becomes_success() -> None:
    class NegativeVerifier(ActionVerifier):
        service_id = "obs_scene_state"

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
            return VerificationResult(False, "obs_websocket", "verification_unknown")

    transport = FakeOBS("Starting")

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(
            transport, verifier=NegativeVerifier(),
        )
        result = await loop.execute(_request(key="rollback"))
        assert result.verified is False
        assert result.error_code == "verification_unknown"
        assert result.result_data["rollback_status"] == "succeeded"
        assert transport.set_calls == ["Main", "Starting"]
        assert transport.scene == "Starting"
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_feature_and_permission_block_before_obs_io() -> None:
    async def scenario() -> None:
        disabled_transport = FakeOBS()
        disabled, _, _, _ = await _build(disabled_transport, enabled=False)
        disabled_result = await disabled.execute(_request(key="disabled"))
        assert disabled_result.error_code == "feature_disabled"
        assert disabled_transport.get_calls == 0
        assert disabled_transport.set_calls == []

        denied_transport = FakeOBS()
        denied, _, _, _ = await _build(
            denied_transport, grant_permission=False,
        )
        health_calls = denied_transport.get_calls
        denied_result = await denied.execute(_request(key="denied"))
        assert denied_result.error_code == "permission_denied"
        assert denied_transport.get_calls == health_calls
        assert denied_transport.set_calls == []

    asyncio.run(scenario())


def test_idempotency_collision_is_rejected_without_second_set() -> None:
    transport = FakeOBS()

    async def scenario() -> None:
        loop, _, _, _ = await _build(transport)
        first = await loop.execute(_request(key="collision", scene="Main"))
        collision = await loop.execute(_request(key="collision", scene="BRB"))
        assert first.verified is True
        assert collision.error_code == "idempotency_conflict"
        assert transport.set_calls == ["Main"]

    asyncio.run(scenario())


def test_world_projection_failure_keeps_verified_external_commit() -> None:
    class RejectingWorld:
        def snapshot(self) -> dict[str, object]: return {"stream": {}}
        def apply_event(self, _event: object) -> bool: return False

    transport = FakeOBS()

    async def scenario() -> None:
        loop, _, transactions, _ = await _build(
            transport, world=RejectingWorld(),
        )
        result = await loop.execute(_request(key="projection-fail"))
        assert result.verified is True
        assert result.error_code == "world_projection_rejected"
        assert result.result_data["world_projected"] is False
        assert transactions.snapshot()["recent"][-1]["state"] == "committed"
        assert transport.scene == "Main"

    asyncio.run(scenario())


def test_cancellation_releases_active_transaction_and_does_not_project_world() -> None:
    transport = FakeOBS()
    transport.block_set = True
    transport.set_entered = asyncio.Event()

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(transport)
        task = asyncio.create_task(loop.execute(_request(key="cancelled")))
        assert transport.set_entered is not None
        await transport.set_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_invalid_target_rejected_before_reservation_and_backend_io() -> None:
    transport = FakeOBS()

    async def scenario() -> None:
        loop, _, transactions, _ = await _build(transport)
        health_calls = transport.get_calls
        bad = replace(_request(key="bad"), arguments={"scene_name": "Other"})
        result = await loop.execute(bad)
        assert result.error_code == "invalid_target"
        assert transport.get_calls == health_calls
        assert transport.set_calls == []
        assert transactions.snapshot()["recent"] == []

    asyncio.run(scenario())


def test_rollback_transport_failure_is_unknown_and_original_action_stays_failed() -> None:
    class NegativeVerifier(ActionVerifier):
        service_id = "obs_scene_state"

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
            return VerificationResult(False, "obs_websocket", "verification_unknown")

    transport = FakeOBS("Starting")
    transport.fail_set_at = 2

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(
            transport, verifier=NegativeVerifier(),
        )
        result = await loop.execute(_request(key="rollback-unknown"))
        assert result.verified is False
        assert result.error_code == "verification_unknown"
        assert result.result_data["rollback_status"] == "unknown"
        assert result.result_data["rollback_reason"] == "rollback_verification_unknown"
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_malformed_executor_result_releases_without_world_projection() -> None:
    transport = FakeOBS()

    async def scenario() -> None:
        loop, world, transactions, executor = await _build(transport)

        async def malformed(_request: ActionRequest) -> object:
            return {"status": "success"}

        executor.execute = malformed  # type: ignore[method-assign]
        result = await loop.execute(_request(key="malformed-executor"))
        assert result.verified is False
        assert result.error_code == "invalid_executor_result"
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_commit_failure_rolls_back_scene_and_releases_transaction() -> None:
    transport = FakeOBS("Starting")

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(transport)

        def fail_commit(_transaction_id: str) -> None:
            raise RuntimeError("commit unavailable")

        transactions.commit = fail_commit  # type: ignore[method-assign]
        result = await loop.execute(_request(key="commit-failed"))
        assert result.verified is False
        assert result.error_code == "final_commit_failed"
        assert result.result_data["rollback_status"] == "succeeded"
        assert transport.scene == "Starting"
        assert world.query("stream.current_scene") is None
        assert transactions.snapshot()["recent"][-1]["state"] == "released"

    asyncio.run(scenario())


def test_commit_exception_after_state_mutation_keeps_verified_scene() -> None:
    transport = FakeOBS("Starting")

    async def scenario() -> None:
        loop, world, transactions, _ = await _build(transport)
        original_commit = transactions.commit

        def commit_then_raise(transaction_id: str) -> None:
            original_commit(transaction_id)
            raise RuntimeError("observer failed after commit")

        transactions.commit = commit_then_raise  # type: ignore[method-assign]
        result = await loop.execute(_request(key="commit-mutated"))
        assert result.verified is True
        assert result.result_data["rollback_status"] == "not_required"
        assert transport.set_calls == ["Main"]
        assert transport.scene == "Main"
        assert world.query("stream.current_scene").value == "Main"
        assert transactions.snapshot()["recent"][-1]["state"] == "committed"

    asyncio.run(scenario())


def test_fake_obs_success_and_failure_replay_are_deterministic() -> None:
    async def replay(*, operator_race: bool) -> tuple[object, ...]:
        transport = FakeOBS()
        if operator_race:
            transport.override_set_scene = "OperatorScene"
        loop, world, transactions, _ = await _build(transport)
        result = await loop.execute(_request(
            key="replay-failure" if operator_race else "replay-success",
        ))
        current = world.query("stream.current_scene")
        return (
            result.status.value,
            result.verified,
            result.error_code,
            result.result_data.get("rollback_status"),
            transactions.snapshot()["recent"][-1]["state"],
            current.value if current is not None else None,
            tuple(transport.set_calls),
        )

    async def scenario() -> None:
        assert await replay(operator_race=False) == await replay(operator_race=False)
        assert await replay(operator_race=True) == await replay(operator_race=True)

    asyncio.run(scenario())


def test_malformed_capability_and_reservation_results_fail_closed_before_obs_io() -> None:
    async def scenario() -> None:
        capability_transport = FakeOBS()
        malformed_capability, _, malformed_transactions, _ = await _build(
            capability_transport,
        )
        health_calls = capability_transport.get_calls
        malformed_capability._capabilities.capability = (  # type: ignore[method-assign]
            lambda _capability_id: {"action_type": "SWITCH_SCENE"}
        )
        capability_result = await malformed_capability.execute(
            _request(key="malformed-capability"),
        )
        assert capability_result.error_code == "capability_source_malformed"
        assert capability_transport.get_calls == health_calls
        assert capability_transport.set_calls == []
        assert malformed_transactions.snapshot()["recent"] == []

        reservation_transport = FakeOBS()
        malformed_reservation, _, transactions, _ = await _build(reservation_transport)
        health_calls = reservation_transport.get_calls
        transactions.reserve = lambda _action, _key: object()  # type: ignore[method-assign]
        reservation_result = await malformed_reservation.execute(
            _request(key="malformed-reservation"),
        )
        assert reservation_result.error_code == "reservation_result_invalid"
        assert reservation_transport.get_calls == health_calls
        assert reservation_transport.set_calls == []

    asyncio.run(scenario())
