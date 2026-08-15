"""External executor registry stays inert until a later adapter phase registers routes."""
from __future__ import annotations

import asyncio

import pytest

from interfaces.action_execution import ActionExecutor, ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.external_executor import ExternalExecutorBinding
from services.action.external_registry import ExternalExecutorRegistry


class _Executor(ActionExecutor):
    service_id = "test_executor"
    async def start(self) -> None: pass
    async def stop(self) -> None: pass
    async def health_check(self) -> HealthStatus: return HealthStatus.healthy(self.service_id)
    def get_metrics(self) -> dict[str, int]: return {}
    async def execute(self, request: ActionRequest) -> ActionResult: raise AssertionError("registry must not execute")


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
    assert registry.snapshot() == {"registered": 0, "bindings": []}
    assert registry.get_metrics() == {
        "external_executor_registry_registered": 0,
        "external_executor_registry_running": 0,
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
    registry.register(_binding(), _Executor(), _Verifier())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_binding(), _Executor(), _Verifier())

def test_empty_registry_lifecycle_is_healthy_and_still_inert() -> None:
    registry = ExternalExecutorRegistry()
    asyncio.run(registry.start())
    assert asyncio.run(registry.health_check()).is_ok is True
    assert registry.get_metrics()["external_executor_registry_running"] == 1
    assert registry.snapshot() == {"registered": 0, "bindings": []}
    asyncio.run(registry.stop())
    assert asyncio.run(registry.health_check()).state.value == "stopped"