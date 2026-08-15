from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dashboard.dashboard_server import DashboardServer
from interfaces.capability import CapabilityRegistryService
from interfaces.compatibility import Capability
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.capability.registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityRegistryConfig,
)


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __call__(self) -> datetime:
        return NOW


class Source:
    def __init__(self, value: object) -> None:
        self.value = value

    def __call__(self) -> object:
        return self.value


def _definition(
    capability_id: str,
    *,
    permissions: tuple[str, ...] = (),
    health_target: str = "local",
    verifier: str = "local",
    world: dict[str, object] | None = None,
    self_state: dict[str, object] | None = None,
    conflicts: tuple[str, ...] = (),
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability=Capability(
            capability_id=capability_id,
            action_type=capability_id,
            description=f"{capability_id} declaration",
            executor_id=health_target,
            verifier_id=verifier,
            risk_level="low",
            required_permissions=permissions,
            parameter_schema={},
            transaction_policy="none",
        ),
        health_target_id=health_target,
        world_equals=world or {},
        self_equals=self_state or {},
        conflict_actions=conflicts,
        mock_only=capability_id.startswith("MOCK"),
    )


def _registry(
    *,
    permissions: tuple[str, ...] = ("chat.read", "call.control"),
    health: object | None = None,
    world: object | None = None,
    self_state: object | None = None,
    transactions: object | None = None,
) -> tuple[CapabilityRegistry, MetricsCollector]:
    config = CapabilityRegistryConfig(
        max_evidence_refs=3,
        granted_permissions=frozenset(permissions),
        definitions=(
            _definition("WAIT"),
            _definition("READ_CHAT", permissions=("chat.read",)),
            _definition("CALL", permissions=("call.control",), world={"call.guest_connected": False}),
            _definition("GESTURE", self_state={"avatar_state.connected": True}),
            _definition("SPEAK", health_target="tts", verifier="speech", conflicts=("SPEAK",)),
            _definition("NEEDS_VERIFIER", verifier="missing"),
            _definition("BAD_HEALTH", health_target="bad"),
        ),
    )
    metrics = MetricsCollector()
    registry = CapabilityRegistry(
        config,
        world_snapshot_provider=Source(world if world is not None else {"call": {"guest_connected": {"value": False}}}),
        self_snapshot_provider=Source(self_state if self_state is not None else {"avatar_state": {"connected": True}}),
        transaction_snapshot_provider=Source(transactions if transactions is not None else {"recent": []}),
        health_snapshot_provider=Source(health if health is not None else {"targets": {"local": {"health": "healthy"}, "tts": {"health": "healthy"}, "bad": {"health": "unhealthy"}}}),
        metrics=metrics,
        clock=Clock(),
    )
    for verifier in ("local", "speech"):
        registry.register_verifier(verifier)
    return registry, metrics


def test_capability_registry_is_deterministic_and_world_self_aware() -> None:
    registry, metrics = _registry()
    assert isinstance(registry, CapabilityRegistryService)
    assert registry.availability("WAIT").reason_code == "available"
    assert registry.availability("READ_CHAT").available is True
    assert registry.availability("CALL").available is True
    assert registry.availability("GESTURE").available is True
    assert registry.availability("CALL").checked_at == NOW
    assert metrics.capability_registry_snapshot()["checks"]["available"] == 5

    blocked_world, _ = _registry(world={"call": {"guest_connected": {"value": True}}})
    blocked_self, _ = _registry(self_state={"avatar_state": {"connected": False}})
    assert blocked_world.availability("CALL").reason_code == "world_precondition_failed"
    assert blocked_self.availability("GESTURE").reason_code == "self_precondition_failed"


def test_capability_registry_denies_permissions_health_verifier_conflict_and_unknown() -> None:
    permission_denied, _ = _registry(permissions=())
    assert permission_denied.availability("READ_CHAT").reason_code == "permission_denied"

    registry, _ = _registry(transactions={"recent": [
        {"action": "SPEAK", "state": "delivering"},
    ]})
    assert registry.availability("BAD_HEALTH").reason_code == "executor_unhealthy"
    assert registry.availability("NEEDS_VERIFIER").reason_code == "missing_verifier"
    assert registry.availability("SPEAK").reason_code == "transaction_conflict"
    assert registry.availability("NOT_DECLARED").reason_code == "unknown_capability"

    registry.set_enabled(False)
    assert registry.availability("WAIT").reason_code == "feature_disabled"


def test_capability_registry_yaml_dashboard_and_director_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = CapabilityRegistryConfig.from_loader(loader)
    assert len(config.definitions) == 11
    assert any(item.capability.capability_id == "WAIT" for item in config.definitions)
    assert loader.get("features", "features.capability_registry.enabled") is True

    registry, _ = _registry()
    dashboard = asyncio.run(DashboardServer(capability_registry=registry).build_snapshot())
    assert dashboard["capabilities"]["snapshot"]["capabilities"][0]["capability"]["capability_id"] == "BAD_HEALTH"

    source = (root / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    director_call = source[source.index("director_loop = DirectorLoop("):source.index("# ─── M9 operator control plane")]
    assert "capability_registry=capability_registry" not in director_call
    template = (root / "dashboard" / "templates" / "operator_v2.html").read_text(encoding="utf-8")
    assert 'id="system-capabilities"' in template
