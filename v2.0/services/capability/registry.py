"""Deterministic, read-only capability availability registry for Phase 4."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from interfaces.base import HealthState, HealthStatus
from interfaces.capability import CapabilityHealthProvider, CapabilityRegistryService
from interfaces.compatibility import Capability, CapabilityAvailability


_ACTIVE_TRANSACTION_STATES = frozenset({"reserved", "generated", "delivering", "delivered"})
_TRANSACTION_STATES = _ACTIVE_TRANSACTION_STATES | frozenset({"committed", "released"})
_HEALTHY = "healthy"
_RISK_LEVELS = frozenset({"low", "medium", "high"})
_TRANSACTION_POLICIES = frozenset({"none", "delivery_aware", "verified"})


@dataclass(frozen=True)
class CapabilityDefinition:
    capability: Capability
    health_target_id: str
    world_equals: Mapping[str, Any]
    self_equals: Mapping[str, Any]
    conflict_actions: tuple[str, ...]
    mock_only: bool
    verifier_health_target_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise ValueError("capability definition needs a Capability")
        if self.capability.risk_level not in _RISK_LEVELS:
            raise ValueError("capability risk_level is unsupported")
        if self.capability.transaction_policy not in _TRANSACTION_POLICIES:
            raise ValueError("capability transaction_policy is unsupported")
        if len(set(self.capability.required_permissions)) != len(
            self.capability.required_permissions
        ):
            raise ValueError("required_permissions must be unique")
        health_target_id = _required(self.health_target_id, "health_target_id")
        verifier_target = (
            health_target_id
            if self.verifier_health_target_id is None
            else _required(self.verifier_health_target_id, "verifier_health_target_id")
        )
        conflicts = _strict_strings(self.conflict_actions, "conflict_actions")
        if len(set(conflicts)) != len(conflicts):
            raise ValueError("conflict_actions must be unique")
        if not isinstance(self.mock_only, bool):
            raise ValueError("mock_only must be boolean")
        object.__setattr__(self, "health_target_id", health_target_id)
        object.__setattr__(self, "verifier_health_target_id", verifier_target)
        object.__setattr__(self, "world_equals", _preconditions(self.world_equals, "world_equals"))
        object.__setattr__(self, "self_equals", _preconditions(self.self_equals, "self_equals"))
        object.__setattr__(self, "conflict_actions", conflicts)


@dataclass(frozen=True)
class CapabilityRegistryConfig:
    max_evidence_refs: int
    granted_permissions: frozenset[str]
    definitions: tuple[CapabilityDefinition, ...]

    def __post_init__(self) -> None:
        max_evidence_refs = _positive_int(
            self.max_evidence_refs, "registry.max_evidence_refs",
        )
        if not isinstance(self.granted_permissions, frozenset):
            raise ValueError("registry.granted_permissions must be a frozenset")
        permissions = _strict_strings(
            tuple(self.granted_permissions), "registry.granted_permissions",
        )
        if len(set(permissions)) != len(permissions):
            raise ValueError("registry.granted_permissions must be unique")
        if not isinstance(self.definitions, tuple) or not self.definitions:
            raise ValueError("capability registry needs declarations")
        if not all(isinstance(item, CapabilityDefinition) for item in self.definitions):
            raise ValueError("registry.definitions must contain CapabilityDefinition values")
        identifiers = tuple(item.capability.capability_id for item in self.definitions)
        action_types = tuple(item.capability.action_type for item in self.definitions)
        if len(set(identifiers)) != len(identifiers) or "WAIT" not in identifiers:
            raise ValueError("capability IDs must be unique and include WAIT")
        if len(set(action_types)) != len(action_types):
            raise ValueError("capability action types must be unique")
        known_actions = set(action_types)
        for definition in self.definitions:
            unknown = set(definition.conflict_actions) - known_actions - {"*"}
            if unknown:
                raise ValueError("conflict_actions must reference declared action types")
        object.__setattr__(self, "max_evidence_refs", max_evidence_refs)
        object.__setattr__(self, "granted_permissions", frozenset(permissions))

    @classmethod
    def from_loader(cls, loader: Any) -> "CapabilityRegistryConfig":
        registry = loader.get("capabilities", "registry", None)
        declarations = loader.get("capabilities", "capabilities", None)
        if not isinstance(registry, Mapping) or not isinstance(declarations, Mapping):
            raise ValueError("capabilities config must use mappings")
        permissions = _strict_strings(
            registry.get("granted_permissions"), "registry.granted_permissions",
            accepted_types=(list, tuple),
        )
        if len(set(permissions)) != len(permissions):
            raise ValueError("registry.granted_permissions must be unique")
        definitions = tuple(
            _definition(capability_id, value)
            for capability_id, value in sorted(
                declarations.items(), key=lambda item: _required(item[0], "capability_id"),
            )
        )
        return cls(
            max_evidence_refs=registry.get("max_evidence_refs"),
            granted_permissions=frozenset(permissions),
            definitions=definitions,
        )


class CapabilityRegistry(CapabilityRegistryService):
    """Evaluate declared capabilities from public snapshots; never execute them."""

    service_id = "capability_registry"

    def __init__(
        self,
        config: CapabilityRegistryConfig,
        *,
        world_snapshot_provider: Callable[[], Any] | None = None,
        self_snapshot_provider: Callable[[], Any] | None = None,
        transaction_snapshot_provider: Callable[[], Any] | None = None,
        health_snapshot_provider: Callable[[], Any] | None = None,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, CapabilityRegistryConfig):
            raise ValueError("config must be CapabilityRegistryConfig")
        self._config = config
        self._definitions = {item.capability.capability_id: item for item in config.definitions}
        self._declared_verifiers = frozenset(
            item.capability.verifier_id for item in config.definitions
        )
        self._declared_executors = frozenset(
            item.capability.executor_id for item in config.definitions
        )
        self._world_snapshot_provider = world_snapshot_provider
        self._self_snapshot_provider = self_snapshot_provider
        self._transaction_snapshot_provider = transaction_snapshot_provider
        self._health_snapshot_provider = health_snapshot_provider
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._verifiers: set[str] = set()
        self._health_providers: dict[str, CapabilityHealthProvider] = {}
        self._checks: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "CapabilityRegistry":
        return cls(CapabilityRegistryConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "capability registry disabled")
        return HealthStatus.healthy(self.service_id, declarations=len(self._definitions))

    def register_verifier(self, verifier_id: str) -> None:
        value = _required(verifier_id, "verifier_id")
        if value not in self._declared_verifiers:
            raise ValueError(f"undeclared verifier_id: {value}")
        self._verifiers.add(value)

    def register_health_provider(
        self, executor_id: str, provider: CapabilityHealthProvider,
    ) -> None:
        value = _required(executor_id, "executor_id")
        if value not in self._declared_executors:
            raise ValueError(f"undeclared executor_id: {value}")
        if not callable(provider):
            raise ValueError("health provider must be callable")
        self._health_providers[value] = provider

    def capability(self, capability_id: str) -> Capability | None:
        value = _optional_identifier(capability_id)
        definition = self._definitions.get(value) if value is not None else None
        return definition.capability if definition is not None else None

    def availability(self, capability_id: str) -> CapabilityAvailability:
        checked_at = _utc(self._clock())
        normalized = _optional_identifier(capability_id)
        result_id = normalized or "unknown"
        if not self._enabled:
            return self._result(result_id, False, "feature_disabled", checked_at)
        definition = self._definitions.get(normalized) if normalized is not None else None
        if definition is None:
            return self._result(result_id, False, "unknown_capability", checked_at)
        capability_id = definition.capability.capability_id
        missing = sorted(set(definition.capability.required_permissions) - self._config.granted_permissions)
        if missing:
            return self._result(capability_id, False, "permission_denied", checked_at, *(f"permission:{item}" for item in missing))
        executor_healthy = self._executor_healthy(definition)
        if not executor_healthy:
            return self._result(capability_id, False, "executor_unhealthy", checked_at, f"executor:{definition.capability.executor_id}")
        if definition.capability.verifier_id not in self._verifiers:
            return self._result(capability_id, False, "missing_verifier", checked_at, f"verifier:{definition.capability.verifier_id}")
        verifier_healthy = (
            executor_healthy
            if definition.verifier_health_target_id == definition.health_target_id
            else self._target_healthy(definition.verifier_health_target_id)
        )
        if not verifier_healthy:
            return self._result(
                capability_id, False, "verifier_unhealthy", checked_at,
                f"verifier:{definition.capability.verifier_id}",
            )
        if self._has_conflict(definition):
            return self._result(capability_id, False, "transaction_conflict", checked_at, "transaction:active")
        if not self._matches(self._world_snapshot_provider, definition.world_equals):
            return self._result(capability_id, False, "world_precondition_failed", checked_at, *definition.world_equals.keys())
        if not self._matches(self._self_snapshot_provider, definition.self_equals):
            return self._result(capability_id, False, "self_precondition_failed", checked_at, *definition.self_equals.keys())
        return self._result(capability_id, True, "available", checked_at)

    def snapshot(self) -> dict[str, Any]:
        entries = []
        for capability_id in sorted(self._definitions):
            definition = self._definitions[capability_id]
            availability = self.availability(capability_id)
            entries.append({
                "capability": definition.capability.to_dict(),
                "availability": availability.to_dict(),
                "mock_only": definition.mock_only,
            })
        try:
            recorder = getattr(self._metrics, "set_capability_registry_counts", None)
            if callable(recorder):
                recorder(
                    len(self._definitions),
                    sum(1 for entry in entries if entry["availability"]["available"]),
                )
        except Exception:
            pass
        return {"enabled": self._enabled, "capabilities": entries}

    def get_metrics(self) -> dict[str, Any]:
        return {
            "capability_registry_enabled": self._enabled,
            "capability_registry_declarations": len(self._definitions),
            "capability_registry_checks": dict(sorted(self._checks.items())),
        }

    def _executor_healthy(self, definition: CapabilityDefinition) -> bool:
        provider = self._health_providers.get(definition.capability.executor_id)
        if provider is not None:
            try:
                return _health_value(provider()) == _HEALTHY
            except Exception:
                return False
        return self._target_healthy(definition.health_target_id)

    def _target_healthy(self, target_id: str | None) -> bool:
        if target_id is None or self._health_snapshot_provider is None:
            return False
        try:
            snapshot = _as_mapping(self._health_snapshot_provider())
        except Exception:
            return False
        if snapshot is None:
            return False
        targets = _as_mapping(snapshot.get("targets"))
        if targets is None:
            return False
        return _health_value(targets.get(target_id)) == _HEALTHY

    def _has_conflict(self, definition: CapabilityDefinition) -> bool:
        if not definition.conflict_actions:
            return False
        if self._transaction_snapshot_provider is None:
            return True
        try:
            snapshot = _as_mapping(self._transaction_snapshot_provider())
        except Exception:
            return True
        if snapshot is None:
            return True
        recent = snapshot.get("recent")
        if not isinstance(recent, (tuple, list)):
            return True
        actions = set(definition.conflict_actions)
        for item in recent:
            if not isinstance(item, Mapping):
                return True
            state = item.get("state")
            action = item.get("action")
            if (
                not isinstance(state, str)
                or state not in _TRANSACTION_STATES
                or not isinstance(action, str)
                or not action.strip()
            ):
                return True
            if state in _ACTIVE_TRANSACTION_STATES and (
                "*" in actions or action.strip() in actions
            ):
                return True
        return False

    def _matches(self, provider: Callable[[], Any] | None, expected: Mapping[str, Any]) -> bool:
        if not expected:
            return True
        if provider is None:
            return False
        try:
            snapshot = _as_mapping(provider())
        except Exception:
            return False
        if snapshot is None:
            return False
        for path, expected_value in expected.items():
            found, actual_value = _path_value(snapshot, path)
            if not found or actual_value != expected_value:
                return False
        return True

    def _result(
        self, capability_id: str, available: bool, reason_code: str,
        checked_at: datetime, *evidence_refs: str,
    ) -> CapabilityAvailability:
        evidence = _strict_strings(evidence_refs, "evidence_refs")[:self._config.max_evidence_refs]
        self._checks[reason_code] = self._checks.get(reason_code, 0) + 1
        try:
            recorder = getattr(self._metrics, "record_capability_availability", None)
            if callable(recorder):
                recorder(reason_code, available, len(self._definitions))
        except Exception:
            pass
        return CapabilityAvailability(capability_id, available, reason_code, checked_at, evidence)


def _definition(capability_id: Any, raw: Any) -> CapabilityDefinition:
    if not isinstance(raw, Mapping):
        raise ValueError("capability declaration must be a mapping")
    capability_id = _required(capability_id, "capability_id")
    capability = Capability(
        capability_id=capability_id,
        action_type=_required(raw.get("action_type"), "action_type"),
        description=_required(raw.get("description"), "description"),
        executor_id=_required(raw.get("executor_id"), "executor_id"),
        verifier_id=_required(raw.get("verifier_id"), "verifier_id"),
        risk_level=_required(raw.get("risk_level"), "risk_level"),
        required_permissions=_strict_strings(
            raw.get("required_permissions"), "required_permissions",
            accepted_types=(list, tuple),
        ),
        parameter_schema=_required_mapping(raw.get("parameter_schema"), "parameter_schema"),
        transaction_policy=_required(raw.get("transaction_policy"), "transaction_policy"),
    )
    health_target_id = _required(raw.get("health_target_id", capability.executor_id), "health_target_id")
    mock_only = raw.get("mock_only", False)
    if not isinstance(mock_only, bool):
        raise ValueError("mock_only must be boolean")
    return CapabilityDefinition(
        capability=capability,
        health_target_id=health_target_id,
        world_equals=_required_mapping(raw.get("world_equals", {}), "world_equals"),
        self_equals=_required_mapping(raw.get("self_equals", {}), "self_equals"),
        conflict_actions=_strict_strings(
            raw.get("conflict_actions"), "conflict_actions",
            accepted_types=(list, tuple),
        ),
        mock_only=mock_only,
        verifier_health_target_id=(
            _required(raw.get("verifier_health_target_id"), "verifier_health_target_id")
            if "verifier_health_target_id" in raw else None
        ),
    )


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        value = converter()
    return value if isinstance(value, Mapping) else None


def _path_value(value: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = value
    for part in path.split("."):
        mapped = _as_mapping(node)
        if mapped is None or part not in mapped:
            return False, None
        node = mapped[part]
    mapped = _as_mapping(node)
    return (True, mapped["value"]) if mapped is not None and "value" in mapped else (True, node)


def _health_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return _HEALTHY if value else "unhealthy"
    if isinstance(value, HealthStatus):
        return value.state.value
    if isinstance(value, HealthState):
        return value.value
    mapped = _as_mapping(value)
    if mapped is None:
        return None
    raw = mapped.get("health", mapped.get("state"))
    if isinstance(raw, HealthState):
        return raw.value
    return raw.strip().lower() if isinstance(raw, str) and raw.strip() else None


def _strict_strings(
    value: Any,
    field_name: str,
    *,
    accepted_types: tuple[type[Any], ...] = (tuple,),
) -> tuple[str, ...]:
    if not isinstance(value, accepted_types):
        raise ValueError(f"{field_name} must be a sequence")
    result = tuple(_required(entry, field_name) for entry in value)
    return result


def _required(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def _optional_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _required_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _preconditions(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    frozen: dict[str, Any] = {}
    for raw_path, expected in value.items():
        path = _required(raw_path, f"{field_name} path")
        if any(not part for part in path.split(".")):
            raise ValueError(f"{field_name} path must not contain empty components")
        if path in frozen:
            raise ValueError(f"{field_name} paths must be unique")
        frozen[path] = _freeze(expected)
    return MappingProxyType(frozen)


def _freeze(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("capability config must contain finite values")
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({
            _required(key, "mapping key"): _freeze(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise ValueError(f"capability config type is not immutable: {type(value).__name__}")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capability registry clock must be timezone-aware")
    return value.astimezone(timezone.utc)
