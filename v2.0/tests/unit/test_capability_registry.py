from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard.dashboard_server import DashboardServer
from interfaces.base import HealthStatus
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


class ConfigSource:
    def __init__(self, registry: object, declarations: object) -> None:
        self.registry = registry
        self.declarations = declarations

    def get(self, _file: str, path: str, default: object = None) -> object:
        if path == "registry":
            return self.registry
        if path == "capabilities":
            return self.declarations
        return default


class BrokenMetrics:
    def record_capability_availability(self, *_args: object) -> None:
        raise RuntimeError("metric unavailable")

    def set_capability_registry_counts(self, *_args: object) -> None:
        raise RuntimeError("metric unavailable")


def _definition(
    capability_id: str,
    *,
    permissions: tuple[str, ...] = (),
    health_target: str = "local",
    verifier: str = "local",
    world: dict[str, object] | None = None,
    self_state: dict[str, object] | None = None,
    conflicts: tuple[str, ...] = (),
    executor: str | None = None,
    verifier_health_target: str | None = None,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability=Capability(
            capability_id=capability_id,
            action_type=capability_id,
            description=f"{capability_id} declaration",
            executor_id=executor or health_target,
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
        verifier_health_target_id=verifier_health_target,
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


def _raw_wait(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "action_type": "WAIT",
        "description": "Wait without side effects.",
        "executor_id": "local_wait",
        "health_target_id": "local_wait",
        "verifier_id": "local_wait",
        "risk_level": "low",
        "required_permissions": [],
        "parameter_schema": {},
        "transaction_policy": "none",
        "conflict_actions": [],
        "mock_only": False,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("registry", "declaration"),
    [
        ({"max_evidence_refs": True, "granted_permissions": []}, _raw_wait()),
        ({"max_evidence_refs": "4", "granted_permissions": []}, _raw_wait()),
        ({"max_evidence_refs": 4, "granted_permissions": [1]}, _raw_wait()),
        ({"max_evidence_refs": 4, "granted_permissions": []}, _raw_wait(mock_only="false")),
        ({"max_evidence_refs": 4, "granted_permissions": []}, _raw_wait(required_permissions=[None])),
        ({"max_evidence_refs": 4, "granted_permissions": []}, _raw_wait(risk_level="critical")),
        ({"max_evidence_refs": 4, "granted_permissions": []}, _raw_wait(transaction_policy="best_effort")),
        ({"max_evidence_refs": 4, "granted_permissions": []}, _raw_wait(world_equals=[])),
    ],
)
def test_capability_registry_config_rejects_coercion_and_invalid_shapes(
    registry: object, declaration: object,
) -> None:
    with pytest.raises(ValueError):
        CapabilityRegistryConfig.from_loader(ConfigSource(registry, {"WAIT": declaration}))

    with pytest.raises(ValueError):
        CapabilityRegistryConfig(0, frozenset(), ())


def test_capability_declaration_is_deeply_immutable_and_inventory_is_exact() -> None:
    expected = {"call.connected": False}
    definition = _definition("WAIT", world=expected)
    expected.clear()
    assert definition.world_equals == {"call.connected": False}
    with pytest.raises(TypeError):
        definition.world_equals["call.connected"] = True  # type: ignore[index]

    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = CapabilityRegistryConfig.from_loader(loader)
    assert {item.capability.capability_id for item in config.definitions} == {
        "SPEAK", "WAIT", "READ_CHAT", "SELF_TALK", "FOLLOW_UP", "AVATAR_GESTURE",
        "PLAY_MUSIC", "STOP_MUSIC", "SWITCH_SCENE", "CALL_GUEST", "REMOVE_GUEST",
    }
    assert {item.capability.capability_id for item in config.definitions if item.mock_only} == {
        "PLAY_MUSIC", "STOP_MUSIC", "CALL_GUEST", "REMOVE_GUEST",
    }
    scene = next(
        item for item in config.definitions
        if item.capability.capability_id == "SWITCH_SCENE"
    )
    assert scene.capability.executor_id == "obs_scene"
    assert scene.capability.verifier_id == "obs_scene_state"
    assert dict(scene.capability.parameter_schema) == {"scene_name": "string"}
    assert scene.mock_only is False
    avatar = next(
        item for item in config.definitions
        if item.capability.capability_id == "AVATAR_GESTURE"
    )
    assert dict(avatar.capability.parameter_schema) == {"gesture_id": "string"}
    assert avatar.conflict_actions == ("AVATAR_GESTURE",)


def test_capability_registry_uses_executor_id_provider_and_strict_boolean_health() -> None:
    definition = _definition(
        "WAIT", executor="wait_executor", health_target="runtime_wait",
    )
    registry = CapabilityRegistry(
        CapabilityRegistryConfig(2, frozenset(), (definition,)),
        health_snapshot_provider=lambda: {
            "targets": {"runtime_wait": {"health": "unhealthy"}},
        },
        clock=Clock(),
    )
    registry.register_verifier("local")
    registry.register_health_provider("wait_executor", lambda: True)
    assert registry.availability("WAIT").reason_code == "available"

    registry.register_health_provider("wait_executor", lambda: False)
    assert registry.availability("WAIT").reason_code == "executor_unhealthy"
    registry.register_health_provider(
        "wait_executor", lambda: HealthStatus.healthy("wait_executor"),
    )
    assert registry.availability("WAIT").reason_code == "available"


def test_capability_registry_requires_registered_and_healthy_verifier() -> None:
    definition = _definition(
        "WAIT", health_target="executor", verifier="verify",
        verifier_health_target="verifier",
    )
    health = Source({
        "targets": {
            "executor": {"health": "healthy"},
            "verifier": {"health": "unhealthy"},
        },
    })
    registry = CapabilityRegistry(
        CapabilityRegistryConfig(2, frozenset(), (definition,)),
        health_snapshot_provider=health, clock=Clock(),
    )
    assert registry.availability("WAIT").reason_code == "missing_verifier"
    registry.register_verifier("verify")
    assert registry.availability("WAIT").reason_code == "verifier_unhealthy"
    health.value = {
        "targets": {
            "executor": {"health": "healthy"},
            "verifier": {"health": "healthy"},
        },
    }
    assert registry.availability("WAIT").reason_code == "available"


@pytest.mark.parametrize(
    "transactions",
    [
        {"recent": [{"action": "SPEAK", "state": "delivered"}]},
        {"recent": [{"action": "SPEAK", "state": "unexpected"}]},
        {"recent": ["malformed"]},
        {"recent": "malformed"},
    ],
)
def test_capability_registry_fails_closed_for_active_or_malformed_transactions(
    transactions: object,
) -> None:
    registry, _ = _registry(transactions=transactions)
    assert registry.availability("SPEAK").reason_code == "transaction_conflict"

    registry._transaction_snapshot_provider = None
    assert registry.availability("SPEAK").reason_code == "transaction_conflict"


def test_capability_registry_missing_path_never_matches_expected_null() -> None:
    definition = _definition("WAIT", world={"call.missing": None})
    registry = CapabilityRegistry(
        CapabilityRegistryConfig(2, frozenset(), (definition,)),
        world_snapshot_provider=lambda: {},
        health_snapshot_provider=lambda: {
            "targets": {"local": {"health": "healthy"}},
        },
        clock=Clock(),
    )
    registry.register_verifier("local")
    assert registry.availability("WAIT").reason_code == "world_precondition_failed"


def test_capability_registry_bounds_registration_and_isolates_metric_failures() -> None:
    registry, _ = _registry()
    with pytest.raises(ValueError, match="undeclared verifier"):
        registry.register_verifier("unknown")
    with pytest.raises(ValueError, match="undeclared executor"):
        registry.register_health_provider("unknown", lambda: True)
    registry.register_verifier("local")
    assert len(registry._verifiers) == 2

    registry._metrics = BrokenMetrics()
    assert registry.availability("WAIT").available is True
    assert registry.snapshot()["capabilities"]


def test_capability_lookup_does_not_coerce_invalid_identifiers() -> None:
    registry, _ = _registry()
    assert registry.capability(123) is None  # type: ignore[arg-type]
    unavailable = registry.availability(None)  # type: ignore[arg-type]
    assert unavailable.capability_id == "unknown"
    assert unavailable.reason_code == "unknown_capability"
