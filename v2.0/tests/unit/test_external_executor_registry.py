"""External executor registry stays inert until a later adapter phase registers routes."""
from __future__ import annotations

import asyncio

import pytest

from interfaces.action_execution import ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.external_executor import (
    ExternalActionExecutor,
    ExternalExecutorBinding,
    RollbackResult,
    RollbackStatus,
)
from services.action.external_registry import ExternalExecutorRegistry


class _Executor(ExternalActionExecutor):
    service_id = "test_executor"
    async def start(self) -> None: pass
    async def stop(self) -> None: pass
    async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
    def get_metrics(self) -> dict[str, int]: return {}
    async def execute(self, request: ActionRequest) -> ActionResult: raise AssertionError("registry must not execute")
    async def rollback(self, request: ActionRequest, result: ActionResult) -> RollbackResult:
        return RollbackResult(RollbackStatus.SKIPPED, "not_required")


class _Verifier(ActionVerifier):
    service_id = "test_verifier"
    async def start(self) -> None: pass
    async def stop(self) -> None: pass
    async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
    def get_metrics(self) -> dict[str, int]: return {}
    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult: raise AssertionError("registry must not verify")


def _binding() -> ExternalExecutorBinding:
    return ExternalExecutorBinding("obs_executor", "obs_verifier", "obs_executor", "obs_api")


def test_empty_registry_is_inert_and_operator_safe() -> None:
    registry = ExternalExecutorRegistry()
    assert registry.executor_for("missing") is None
    assert registry.verifier_for("missing") is None
    assert registry.snapshot() == {
        "registered": 0, "capacity": 8, "running": False, "bindings": [],
    }
    assert registry.get_metrics() == {
        "external_executor_registry_registered": 0,
        "external_executor_registry_capacity": 8,
        "external_executor_registry_running": 0,
        "external_executor_registry_lifecycle_errors_total": 0,
    }


def test_registers_typed_route_without_invoking_it() -> None:
    registry = ExternalExecutorRegistry()
    executor = _Executor()
    verifier = _Verifier()
    registry.register(_binding(), executor, verifier)

    assert registry.executor_for("obs_executor") is executor
    assert registry.verifier_for("obs_verifier") is verifier
    assert registry.snapshot()["bindings"] == [{
        "executor_id": "obs_executor", "verifier_id": "obs_verifier",
        "feature_id": "obs_executor", "health_target_id": "obs_api",
    }]


def test_rejects_duplicate_route_ids() -> None:
    registry = ExternalExecutorRegistry()
    executor = _Executor()
    verifier = _Verifier()
    registry.register(_binding(), executor, verifier)
    registry.register(_binding(), executor, verifier)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            ExternalExecutorBinding(
                "obs_executor", "obs_verifier", "different_feature", "obs_api",
            ),
            executor,
            verifier,
        )
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_binding(), _Executor(), _Verifier())


def test_binding_and_registry_reject_coercion_unknown_route_and_capacity() -> None:
    with pytest.raises(ValueError, match="canonical"):
        ExternalExecutorBinding(" obs_executor ", "obs_verifier", "obs_executor", "obs_api")
    with pytest.raises(ValueError, match="non-empty string"):
        ExternalExecutorBinding(None, "obs_verifier", "obs_executor", "obs_api")  # type: ignore[arg-type]

    allowed = _binding()
    registry = ExternalExecutorRegistry(1, allowed_bindings=(allowed,))
    with pytest.raises(ValueError, match="not declared"):
        registry.register(
            ExternalExecutorBinding("other", "other_v", "other_f", "other_h"),
            _Executor(),
            _Verifier(),
        )
    registry.register(allowed, _Executor(), _Verifier())
    with pytest.raises(ValueError, match="capacity"):
        open_registry = ExternalExecutorRegistry(1)
        open_registry.register(allowed, _Executor(), _Verifier())
        open_registry.register(
            ExternalExecutorBinding("second", "second_v", "second_f", "second_h"),
            _Executor(),
            _Verifier(),
        )


def test_registry_rejects_registration_while_running_and_invalid_lookup_types() -> None:
    registry = ExternalExecutorRegistry()
    asyncio.run(registry.start())
    with pytest.raises(ValueError, match="while running"):
        registry.register(_binding(), _Executor(), _Verifier())
    assert registry.executor_for(None) is None  # type: ignore[arg-type]
    assert registry.verifier_for(7) is None  # type: ignore[arg-type]

def test_empty_registry_lifecycle_is_degraded_and_still_inert() -> None:
    registry = ExternalExecutorRegistry()
    asyncio.run(registry.start())
    assert asyncio.run(registry.health_check()).state.value == "degraded"
    assert registry.get_metrics()["external_executor_registry_running"] == 1
    assert registry.snapshot() == {
        "registered": 0, "capacity": 8, "running": True, "bindings": [],
    }
    asyncio.run(registry.stop())
    assert asyncio.run(registry.health_check()).state.value == "stopped"
