"""Deterministic, read-only capability availability registry for Phase 4."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthState, HealthStatus
from interfaces.capability import CapabilityHealthProvider, CapabilityRegistryService
from interfaces.compatibility import Capability, CapabilityAvailability


_ACTIVE_TRANSACTION_STATES = frozenset({"reserved", "generated", "delivering"})
_HEALTHY = "healthy"


@dataclass(frozen=True)
class CapabilityDefinition:
    capability: Capability
    health_target_id: str
    world_equals: Mapping[str, Any]
    self_equals: Mapping[str, Any]
    conflict_actions: tuple[str, ...]
    mock_only: bool


@dataclass(frozen=True)
class CapabilityRegistryConfig:
    max_evidence_refs: int
    granted_permissions: frozenset[str]
    definitions: tuple[CapabilityDefinition, ...]

    @classmethod
    def from_loader(cls, loader: Any) -> "CapabilityRegistryConfig":
        registry = loader.get("capabilities", "registry", {}) or {}
        declarations = loader.get("capabilities", "capabilities", {}) or {}
        if not isinstance(registry, Mapping) or not isinstance(declarations, Mapping):
            raise ValueError("capabilities config must use mappings")
        max_evidence_refs = int(registry.get("max_evidence_refs", 0))
        permissions = frozenset(_strings(registry.get("granted_permissions", ())))
        definitions = tuple(
            _definition(capability_id, value)
            for capability_id, value in sorted(declarations.items())
        )
        if max_evidence_refs <= 0 or not definitions:
            raise ValueError("capability registry needs positive bounds and declarations")
        ids = [item.capability.capability_id for item in definitions]
        if len(set(ids)) != len(ids) or "WAIT" not in ids:
            raise ValueError("capability IDs must be unique and include WAIT")
        return cls(max_evidence_refs, permissions, definitions)


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
        self._config = config
        self._definitions = {item.capability.capability_id: item for item in config.definitions}
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
        self._verifiers.add(value)

    def register_health_provider(
        self, executor_id: str, provider: CapabilityHealthProvider,
    ) -> None:
        self._health_providers[_required(executor_id, "executor_id")] = provider

    def capability(self, capability_id: str) -> Capability | None:
        definition = self._definitions.get(str(capability_id).strip())
        return definition.capability if definition is not None else None

    def availability(self, capability_id: str) -> CapabilityAvailability:
        checked_at = _utc(self._clock())
        capability_id = str(capability_id).strip()
        if not self._enabled:
            return self._result(capability_id or "unknown", False, "feature_disabled", checked_at)
        definition = self._definitions.get(capability_id)
        if definition is None:
            return self._result(capability_id or "unknown", False, "unknown_capability", checked_at)
        missing = sorted(set(definition.capability.required_permissions) - self._config.granted_permissions)
        if missing:
            return self._result(capability_id, False, "permission_denied", checked_at, *(f"permission:{item}" for item in missing))
        if not self._executor_healthy(definition):
            return self._result(capability_id, False, "executor_unhealthy", checked_at, f"executor:{definition.health_target_id}")
        if definition.capability.verifier_id not in self._verifiers:
            return self._result(capability_id, False, "missing_verifier", checked_at, f"verifier:{definition.capability.verifier_id}")
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
        if self._metrics is not None and hasattr(self._metrics, "set_capability_registry_counts"):
            self._metrics.set_capability_registry_counts(
                len(self._definitions), sum(1 for entry in entries if entry["availability"]["available"]),
            )
        return {"enabled": self._enabled, "capabilities": entries}

    def get_metrics(self) -> dict[str, Any]:
        return {
            "capability_registry_enabled": self._enabled,
            "capability_registry_declarations": len(self._definitions),
            "capability_registry_checks": dict(sorted(self._checks.items())),
        }

    def _executor_healthy(self, definition: CapabilityDefinition) -> bool:
        provider = self._health_providers.get(definition.health_target_id)
        if provider is not None:
            try:
                return _health_value(provider()) == _HEALTHY
            except Exception:
                return False
        try:
            snapshot = _mapping(self._health_snapshot_provider() if self._health_snapshot_provider else None)
        except Exception:
            return False
        targets = _mapping(snapshot.get("targets"))
        return _health_value(targets.get(definition.health_target_id)) == _HEALTHY

    def _has_conflict(self, definition: CapabilityDefinition) -> bool:
        if not definition.conflict_actions:
            return False
        try:
            snapshot = _mapping(self._transaction_snapshot_provider() if self._transaction_snapshot_provider else None)
        except Exception:
            return True
        recent = snapshot.get("recent", ())
        if not isinstance(recent, (tuple, list)):
            return True
        actions = set(definition.conflict_actions)
        return any(
            isinstance(item, Mapping)
            and str(item.get("state", "")) in _ACTIVE_TRANSACTION_STATES
            and ("*" in actions or str(item.get("action", "")) in actions)
            for item in recent
        )

    def _matches(self, provider: Callable[[], Any] | None, expected: Mapping[str, Any]) -> bool:
        if not expected:
            return True
        if provider is None:
            return False
        try:
            snapshot = _mapping(provider())
        except Exception:
            return False
        return all(_path_value(snapshot, path) == value for path, value in expected.items())

    def _result(
        self, capability_id: str, available: bool, reason_code: str,
        checked_at: datetime, *evidence_refs: str,
    ) -> CapabilityAvailability:
        evidence = tuple(_strings(evidence_refs))[:self._config.max_evidence_refs]
        self._checks[reason_code] = self._checks.get(reason_code, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_capability_availability"):
            self._metrics.record_capability_availability(reason_code, available, len(self._definitions))
        return CapabilityAvailability(capability_id, available, reason_code, checked_at, evidence)


def _definition(capability_id: Any, raw: Any) -> CapabilityDefinition:
    if not isinstance(raw, Mapping):
        raise ValueError("capability declaration must be a mapping")
    capability_id = _required(capability_id, "capability_id")
    capability = Capability(
        capability_id=capability_id,
        action_type=_required(raw.get("action_type", capability_id), "action_type"),
        description=_required(raw.get("description"), "description"),
        executor_id=_required(raw.get("executor_id"), "executor_id"),
        verifier_id=_required(raw.get("verifier_id"), "verifier_id"),
        risk_level=_required(raw.get("risk_level", "low"), "risk_level"),
        required_permissions=tuple(_strings(raw.get("required_permissions", ()))),
        parameter_schema=_mapping(raw.get("parameter_schema", {})),
        transaction_policy=_required(raw.get("transaction_policy", "none"), "transaction_policy"),
    )
    health_target_id = _required(raw.get("health_target_id", capability.executor_id), "health_target_id")
    return CapabilityDefinition(
        capability=capability,
        health_target_id=health_target_id,
        world_equals=_mapping(raw.get("world_equals", {})),
        self_equals=_mapping(raw.get("self_equals", {})),
        conflict_actions=tuple(_strings(raw.get("conflict_actions", ()))),
        mock_only=bool(raw.get("mock_only", False)),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return value if isinstance(value, Mapping) else {}


def _path_value(value: Mapping[str, Any], path: str) -> Any:
    node: Any = value
    for part in str(path).split("."):
        node = _mapping(node).get(part)
        if node is None:
            return None
    mapped = _mapping(node)
    return mapped.get("value") if "value" in mapped else node


def _health_value(value: Any) -> str:
    if isinstance(value, HealthStatus):
        return value.state.value
    mapped = _mapping(value)
    return str(mapped.get("health", mapped.get("state", value))).lower()


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return ()
    return tuple(item for item in (str(entry).strip() for entry in value) if item)


def _required(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capability registry clock must be timezone-aware")
    return value.astimezone(timezone.utc)
