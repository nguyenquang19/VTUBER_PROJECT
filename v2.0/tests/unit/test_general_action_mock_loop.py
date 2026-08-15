from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.dashboard_server import DashboardServer
from interfaces.action_execution import ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, Capability, EventProvenance, PerceptionEvent
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
            definitions=(capability("CALL_GUEST", False), capability("REMOVE_GUEST", True)),
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


def _loop(*, outcome: str = "success", world: WorldModelShadow | None = None) -> tuple[GeneralActionMockLoop, WorldModelShadow, ActionTransactionManager, MetricsCollector]:
    actual_world = world or _world()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(actual_world, transactions)
    metrics = MetricsCollector()
    backend = MockCallBackend(default_outcome=outcome)
    loop = GeneralActionMockLoop(
        ActionMockConfig(execution_timeout_s=0.1, max_recent_results=8, default_outcome=outcome),
        capability_registry=registry, transactions=transactions, world_model=actual_world,
        metrics=metrics, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
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

    loop, world, transactions, _ = _loop()

    class UnknownVerifier(ActionVerifier):
        service_id = "unknown"
        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
        def get_metrics(self) -> dict[str, int]: return {}
        async def verify(self, request: ActionRequest, result):
            return VerificationResult(False, self.service_id, "verification_unknown")

    loop.register_verifier("mock_call", UnknownVerifier())
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
    backend = MockCallBackend(default_outcome="success", outcome_provider=outcome)
    loop = GeneralActionMockLoop(
        ActionMockConfig(execution_timeout_s=0.1, max_recent_results=8, default_outcome="success"),
        capability_registry=registry, transactions=transactions, world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())

    bad = replace(_request("CALL_GUEST", key="bad"), arguments={})
    assert asyncio.run(loop.execute(bad)).error_code == "invalid_arguments"
    first = asyncio.run(loop.execute(_request("CALL_GUEST", key="same")))
    duplicate = asyncio.run(loop.execute(_request("CALL_GUEST", key="same")))
    assert first == duplicate
    assert calls == 1


def test_mock_loop_releases_when_world_update_is_rejected_and_dashboard_is_read_only() -> None:
    class RejectingWorld:
        def snapshot(self) -> dict[str, object]:
            return {"call": {"guest_connected": {"value": False}}}
        def apply_event(self, event: PerceptionEvent) -> bool:
            return False

    world = RejectingWorld()
    transactions = ActionTransactionManager(clock=lambda: 1.0)
    registry = _registry(world, transactions)  # type: ignore[arg-type]
    backend = MockCallBackend(default_outcome="success")
    loop = GeneralActionMockLoop(
        ActionMockConfig(execution_timeout_s=0.1, max_recent_results=8, default_outcome="success"),
        capability_registry=registry, transactions=transactions, world_model=world, clock=Clock(),
    )
    loop.register_executor("mock_call", MockCallExecutor(backend, clock=Clock()))
    loop.register_verifier("mock_call", MockCallVerifier(backend))
    asyncio.run(loop.start())
    result = asyncio.run(loop.execute(_request("CALL_GUEST", key="world-fail")))
    assert result.error_code == "world_update_failed"
    assert transactions.snapshot()["recent"][-1]["state"] == "released"
    dashboard = asyncio.run(DashboardServer(action_mock_loop=loop).build_snapshot())
    assert dashboard["action_mock"]["snapshot"]["recent"][-1]["error_code"] == "world_update_failed"


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
