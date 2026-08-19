from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dashboard.dashboard_server import DashboardServer
from interfaces.action_execution import ActionExecutor, ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import (
    ActionRequest, ActionResult, Capability, EventProvenance, PerceptionEvent,
)
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.action.mock_backend import MockCallBackend, MockCallExecutor, MockCallVerifier
from services.action.mock_loop import ActionMockConfig, GeneralActionMockLoop
from services.capability.registry import CapabilityDefinition, CapabilityRegistry, CapabilityRegistryConfig
from services.director.action_transaction import ActionTransactionManager
from services.world.world_model import WorldModelConfig, WorldModelShadow


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __call__(self) -> datetime:
        return NOW


def _world() -> WorldModelShadow:
    model = WorldModelShadow(WorldModelConfig(
        allowed_domains=("call",), default_ttl_s=60.0,
        source_authority={"runtime": 60}, max_state_entries=4,
        max_evidence_refs=4, max_payload_items=4, max_payload_chars=200,
        dedup_ttl_s=60.0, max_dedup_keys=8,
    ), clock=Clock())
    assert model.apply_event(PerceptionEvent(
        schema_version=1, event_id="call:false", source="runtime",
        event_type="world.observation", timestamp=NOW - timedelta(seconds=1),
        payload={"path": "call.guest_connected", "value": False},
        provenance=EventProvenance(producer="test"), confidence=1.0,
    ))
    return model


def _registry(world: WorldModelShadow, transactions: ActionTransactionManager) -> CapabilityRegistry:
    wait = CapabilityDefinition(
        capability=Capability(
            capability_id="WAIT", action_type="WAIT", description="wait declaration",
            executor_id="mock_call", verifier_id="mock_call", risk_level="low",
            required_permissions=(), parameter_schema={}, transaction_policy="none",
        ),
        health_target_id="mock_external", world_equals={}, self_equals={},
        conflict_actions=(), mock_only=False,
    )

    def capability(capability_id: str, expected: bool) -> CapabilityDefinition:
        value = Capability(
            capability_id=capability_id, action_type=capability_id,
            description="mock guest action", executor_id="mock_call", verifier_id="mock_call",
            risk_level="high", required_permissions=("call.control",),
            parameter_schema={"guest_id": "string"}, transaction_policy="verified",
        )
        return CapabilityDefinition(
            capability=value, health_target_id="mock_external",
            world_equals={"call.guest_connected": expected}, self_equals={},
            conflict_actions=("CALL_GUEST", "REMOVE_GUEST"), mock_only=True,
        )

    registry = CapabilityRegistry(
        CapabilityRegistryConfig(
            max_evidence_refs=4, granted_permissions=frozenset({"call.control"}),
            definitions=(
                wait, capability("CALL_GUEST", False), capability("REMOVE_GUEST", True),
            ),
        ),
        world_snapshot_provider=world.snapshot,
        transaction_snapshot_provider=transactions.snapshot,
        health_snapshot_provider=lambda: {"targets": {"mock_external": {"health": "healthy"}}},
        clock=Clock(),
    )
    registry.register_verifier("mock_call")
    return registry


def _request(action: str, *, key: str = "guest:evil") -> ActionRequest:
    return ActionRequest(
        schema_version=1, action_id=f"action:{action}:{key}", capability_id=action,
        action_type=action, target="Evil", arguments={"guest_id": "Evil"},
        intention_id=None, evidence_refs=(), idempotency_key=key, priority=0.0,
        requested_at=NOW, transaction_policy="verified",
    )


def _config(*, outcome: str = "success", **changes: object) -> ActionMockConfig:
    values: dict[str, object] = {
        "execution_timeout_s": 0.1,
        "max_recent_results": 8,
        "max_idempotency_records": 16,
        "max_connected_guests": 2,
        "max_verification_evidence_refs": 4,
        "default_outcome": outcome,
    }
    values.update(changes)
    return ActionMockConfig(**values)  # type: ignore[arg-type]


def _loop(
    *, outcome: str = "success", world: WorldModelShadow | None = None,
    executor: ActionExecutor | None = None,
    verifier: ActionVerifier | None = None,
) -> tuple[GeneralActionMockLoop, WorldModelShadow, ActionTransactionManager, MetricsCollector]:
    actual_world = world or _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(actual_world, transactions)
    metrics = MetricsCollector()
    backend = MockCallBackend(default_outcome=outcome, max_connected_guests=2)
    loop = GeneralActionMockLoop(
        _config(outcome=outcome),
        capability_registry=registry, transactions=transactions, world_model=actual_world,
        metrics=metrics, clock=Clock(),
    )
    loop.register_executor("mock_call", executor or MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", verifier or MockCallVerifier(backend))
    asyncio.run(loop.start())
    return loop, actual_world, transactions, metrics


def test_mock_call_success_updates_world_then_switches_capability() -> None:
    loop, world, transactions, metrics = _loop()
    result = asyncio.run(loop.execute(_request("CALL_GUEST")))

    assert result.verified is True
    assert result.verification_source == "mock_call"
    assert world.query("call.guest_connected").value is True  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"
    assert loop._registry.availability("CALL_GUEST").available is False
    assert loop._registry.availability("REMOVE_GUEST").available is True
    assert metrics.action_mock_snapshot()["outcomes"]["verified"] == 1


def test_mock_call_failure_and_verification_unknown_release_without_world_success() -> None:
    failed_loop, failed_world, failed_transactions, _ = _loop(outcome="failed")
    failed = asyncio.run(failed_loop.execute(_request("CALL_GUEST")))
    assert failed.verified is False
    assert failed.error_code == "mock_failed"
    assert failed_world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert failed_transactions.snapshot()["recent"][-1]["state"] == "released"

    class UnknownVerifier(ActionVerifier):
        service_id = "unknown"
        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(self, request: ActionRequest, result):
            return VerificationResult(False, self.service_id, "verification_unknown")

    loop, world, transactions, _ = _loop(verifier=UnknownVerifier())
    unknown = asyncio.run(loop.execute(_request("CALL_GUEST", key="unknown")))
    assert unknown.error_code == "verification_unknown"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_mock_loop_rejects_bad_request_and_deduplicates_without_second_execution() -> None:
    calls = 0

    def outcome(_request: ActionRequest) -> str:
        nonlocal calls
        calls += 1
        return "success"

    world = _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(world, transactions)
    backend = MockCallBackend(
        default_outcome="success", max_connected_guests=2, outcome_provider=outcome,
    )
    loop = GeneralActionMockLoop(
        _config(),
        capability_registry=registry, transactions=transactions, world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())

    bad = replace(_request("CALL_GUEST", key="bad"), arguments={})
    assert asyncio.run(loop.execute(bad)).error_code == "invalid_target"
    first = asyncio.run(loop.execute(_request("CALL_GUEST", key="same")))
    duplicate = asyncio.run(loop.execute(_request("CALL_GUEST", key="same")))
    assert first == duplicate
    assert calls == 1


def test_mock_loop_rejects_delivered_transaction_conflict_before_executor() -> None:
    loop, world, transactions, metrics = _loop()
    occupied = transactions.reserve("CALL_GUEST", "occupied").transaction
    transactions.mark_generated(occupied.transaction_id)
    transactions.mark_delivering(occupied.transaction_id)
    transactions.mark_delivered(occupied.transaction_id)

    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="blocked")))

    assert result.verified is False
    assert result.error_code == "transaction_conflict"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert metrics.action_mock_snapshot()["outcomes"]["rejected"] == 1


def test_mock_loop_keeps_commit_when_world_projection_is_rejected_and_dashboard_is_read_only() -> None:
    class RejectingWorld:
        def snapshot(self) -> dict[str, object]:
            return {"call": {"guest_connected": {"value": False}}}
        def apply_event(self, event: PerceptionEvent) -> bool:
            return False

    world = RejectingWorld()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(world, transactions)  # type: ignore[arg-type]
    metrics = MetricsCollector()
    backend = MockCallBackend(default_outcome="success", max_connected_guests=2)
    loop = GeneralActionMockLoop(
        _config(),
        capability_registry=registry, transactions=transactions, world_model=world,
        metrics=metrics, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="world-fail")))
    assert result.verified is True
    assert result.result_data["world_projected"] is False
    assert result.error_code == "world_projection_rejected"
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"
    assert loop.get_metrics()["action_mock_world_projection_inconsistencies"] == 1
    assert metrics.action_mock_snapshot()["world_projection_inconsistencies"] == 1
    dashboard = asyncio.run(DashboardServer(action_mock_loop=loop).build_snapshot())
    assert dashboard["action_mock"]["snapshot"]["recent"][-1]["error_code"] == "world_projection_rejected"


def test_mock_loop_yaml_and_current_director_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    assert ActionMockConfig.from_loader(loader).default_outcome == "success"
    assert loader.get("features", "features.action_mock_closed_loop.enabled") is True
    source = (root / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    director_call = source[source.index("director_loop = DirectorLoop("):source.index("# ─── M9 operator control plane")]
    assert "action_mock_loop=action_mock_loop" not in director_call
    template = (root / "dashboard" / "templates" / "operator_v2.html").read_text(encoding="utf-8")
    assert 'id="system-action-mock"' in template


def test_final_commit_failure_releases_without_world_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, world, transactions, _ = _loop()

    def fail_commit(_transaction_id: str) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(transactions, "commit", fail_commit)
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="commit-fail")))

    assert result.error_code == "final_commit_failed"
    assert result.verified is False
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_world_projection_observes_committed_transaction() -> None:
    inner_world = _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)

    class OrderedWorld:
        projections = 0

        def snapshot(self) -> Any:
            return inner_world.snapshot()

        def query(self, path: str) -> Any:
            return inner_world.query(path)

        def apply_event(self, event: PerceptionEvent) -> bool:
            assert transactions.snapshot()["recent"][-1]["state"] == "committed"
            self.projections += 1
            return inner_world.apply_event(event)

    world = OrderedWorld()
    registry = _registry(world, transactions)  # type: ignore[arg-type]
    backend = MockCallBackend(default_outcome="success", max_connected_guests=2)
    loop = GeneralActionMockLoop(
        _config(), capability_registry=registry, transactions=transactions,
        world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())

    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="ordered")))

    assert result.verified is True
    assert world.projections == 1
    assert world.query("call.guest_connected").value is True


def test_commit_exception_after_state_mutation_is_not_released(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, world, transactions, _ = _loop()
    original_commit = transactions.commit

    def commit_then_raise(transaction_id: str) -> None:
        original_commit(transaction_id)
        raise RuntimeError("observer failed after commit")

    monkeypatch.setattr(transactions, "commit", commit_then_raise)
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="commit-mutated")))

    assert result.verified is True
    assert result.result_data["world_projected"] is True
    assert world.query("call.guest_connected").value is True  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"


def test_idempotency_fingerprint_rejects_collision_and_preserves_original() -> None:
    loop, _world_model, _transactions, _metrics = _loop()
    first = asyncio.run(loop.execute(_request("CALL_GUEST", key="collision")))
    collision = asyncio.run(loop.execute(_request("REMOVE_GUEST", key="collision")))
    duplicate = asyncio.run(loop.execute(_request("CALL_GUEST", key="collision")))

    assert first.verified is True
    assert collision.error_code == "idempotency_conflict"
    assert collision.action_id != first.action_id
    assert duplicate == first


def test_metric_failure_cannot_hide_committed_terminal_result() -> None:
    loop, world, transactions, _ = _loop()

    class BrokenMetrics:
        def record_action_mock_outcome(self, _outcome: str) -> None:
            raise RuntimeError("metrics unavailable")

    loop._metrics = BrokenMetrics()
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="metric-fail")))

    assert result.verified is True
    assert world.query("call.guest_connected").value is True  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"
    assert loop.snapshot()["recent"][-1]["action_id"] == result.action_id


def test_invalid_verifier_result_releases_transaction() -> None:
    class InvalidVerifier(ActionVerifier):
        service_id = "invalid"

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(self, request: ActionRequest, result: ActionResult) -> object:
            return {"verified": "false"}

    loop, world, transactions, _ = _loop(verifier=InvalidVerifier())
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="invalid-verifier")))

    assert result.error_code == "invalid_verification_result"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_invalid_executor_result_releases_transaction() -> None:
    class InvalidExecutor(ActionExecutor):
        service_id = "invalid"

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def execute(self, request: ActionRequest) -> object:
            return {"action_id": request.action_id, "status": "success"}

    loop, world, transactions, _ = _loop(executor=InvalidExecutor())
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="invalid-executor")))

    assert result.error_code == "invalid_executor_result"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_cancellation_releases_active_transaction_and_propagates() -> None:
    class BlockingExecutor(ActionExecutor):
        service_id = "blocking"

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def execute(self, request: ActionRequest) -> ActionResult:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    executor = BlockingExecutor()
    loop, world, transactions, _ = _loop(executor=executor)

    async def scenario() -> None:
        task = asyncio.create_task(loop.execute(_request("CALL_GUEST", key="cancelled")))
        await executor.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_target_mismatch_is_rejected_before_reservation() -> None:
    loop, world, transactions, _ = _loop()
    request = replace(
        _request("CALL_GUEST", key="target-mismatch"),
        target="Alice",
        arguments={"guest_id": "Evil"},
    )

    result = asyncio.run(loop.execute(request))

    assert result.error_code == "invalid_target"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"] == []


def test_transaction_lookup_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, world, transactions, _ = _loop()

    def fail_lookup(_idempotency_key: str) -> None:
        raise RuntimeError("transaction source unavailable")

    monkeypatch.setattr(transactions, "find_by_idempotency_key", fail_lookup)
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="tx-source-fail")))

    assert result.error_code == "transaction_source_failed"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"] == []


def test_verification_result_and_config_are_strict() -> None:
    with pytest.raises(ValueError, match="verified must be a bool"):
        VerificationResult("false", "mock_call", "unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source must be"):
        VerificationResult(False, None, "unknown")
    with pytest.raises(ValueError, match="evidence_refs must be a tuple"):
        VerificationResult(True, "mock_call", "verified", ["e1"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_recent_results"):
        _config(max_recent_results=True)
    with pytest.raises(ValueError, match="execution_timeout_s"):
        _config(execution_timeout_s="0.1")
    with pytest.raises(ValueError, match="cover max_recent_results"):
        _config(max_idempotency_records=1)


def test_registration_and_backend_state_are_bounded() -> None:
    loop, _world_model, _transactions, _metrics = _loop()
    backend = MockCallBackend(default_outcome="success", max_connected_guests=1)
    with pytest.raises(ValueError, match="duplicate executor_id"):
        loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    with pytest.raises(ValueError, match="undeclared mock executor_id"):
        loop.register_executor("unknown", MockCallExecutor(backend, clock=Clock()))

    first = asyncio.run(backend.apply(_request("CALL_GUEST", key="guest-one")))
    second_request = replace(
        _request("CALL_GUEST", key="guest-two"),
        action_id="action:CALL_GUEST:guest-two",
        target="Alice",
        arguments={"guest_id": "Alice"},
    )
    second = asyncio.run(backend.apply(second_request))
    assert first == (True, "success")
    assert second == (False, "capacity_exceeded")


def test_idempotency_ledger_survives_recent_result_eviction() -> None:
    calls = 0

    def outcome(_request: ActionRequest) -> str:
        nonlocal calls
        calls += 1
        return "success"

    world = _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(world, transactions)
    backend = MockCallBackend(
        default_outcome="success", max_connected_guests=2, outcome_provider=outcome,
    )
    loop = GeneralActionMockLoop(
        _config(max_recent_results=1, max_idempotency_records=3),
        capability_registry=registry, transactions=transactions, world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())

    original = asyncio.run(loop.execute(_request("CALL_GUEST", key="retained")))
    for key in ("invalid-one", "invalid-two"):
        invalid = replace(
            _request("CALL_GUEST", key=key),
            target="Alice",
            arguments={"guest_id": "Evil"},
        )
        assert asyncio.run(loop.execute(invalid)).error_code == "invalid_target"
    assert len(loop.snapshot()["recent"]) == 1

    assert asyncio.run(loop.execute(_request("CALL_GUEST", key="retained"))) == original
    assert calls == 1


def test_too_many_verification_evidence_refs_releases_without_world_projection() -> None:
    class VerboseVerifier(ActionVerifier):
        service_id = "verbose"

        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(
            self, request: ActionRequest, result: ActionResult,
        ) -> VerificationResult:
            return VerificationResult(
                True, self.service_id, "verified", tuple(f"e:{index}" for index in range(5)),
            )

    loop, world, transactions, _ = _loop(verifier=VerboseVerifier())
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="too-much-evidence")))

    assert result.error_code == "too_many_verification_evidence_refs"
    assert world.query("call.guest_connected").value is False  # type: ignore[union-attr]
    assert transactions.snapshot()["recent"][-1]["state"] == "released"


def test_world_projection_exception_keeps_verified_commit() -> None:
    class FailingWorld:
        def snapshot(self) -> dict[str, object]:
            return {"call": {"guest_connected": {"value": False}}}

        def apply_event(self, event: PerceptionEvent) -> bool:
            raise RuntimeError("world unavailable")

    world = FailingWorld()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(world, transactions)  # type: ignore[arg-type]
    backend = MockCallBackend(default_outcome="success", max_connected_guests=2)
    loop = GeneralActionMockLoop(
        _config(), capability_registry=registry, transactions=transactions,
        world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())

    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="world-exception")))

    assert result.verified is True
    assert result.error_code == "world_projection_exception"
    assert result.result_data["world_projected"] is False
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"execution_timeout_s": "1.0", "max_recent_results": 1,
          "max_idempotency_records": 1, "max_connected_guests": 1,
          "max_verification_evidence_refs": 1, "default_outcome": "success"},
         "execution_timeout_s"),
        ({"execution_timeout_s": 1.0, "max_recent_results": True,
          "max_idempotency_records": 1, "max_connected_guests": 1,
          "max_verification_evidence_refs": 1, "default_outcome": "success"},
         "max_recent_results"),
        ({"execution_timeout_s": 1.0, "max_recent_results": 1,
          "max_idempotency_records": 1, "max_connected_guests": 1,
          "max_verification_evidence_refs": 1, "default_outcome": "SUCCESS"},
         "default_outcome"),
    ],
)
def test_loader_rejects_action_mock_config_coercion(
    raw: dict[str, object], message: str,
) -> None:
    class Loader:
        def get(self, *_args: object) -> dict[str, object]:
            return raw

    with pytest.raises(ValueError, match=message):
        ActionMockConfig.from_loader(Loader())
