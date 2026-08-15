"""Inert registry for future real external executors."""
from __future__ import annotations

from typing import Any

from interfaces.action_execution import ActionExecutor, ActionVerifier
from interfaces.base import HealthStatus
from interfaces.external_executor import ExternalExecutorBinding, ExternalExecutorRegistryService


class ExternalExecutorRegistry(ExternalExecutorRegistryService):
    """Store complete routes only; execution remains owned by a later coordinator."""

    service_id = "external_executor_registry"

    def __init__(self) -> None:
        self._bindings: dict[str, ExternalExecutorBinding] = {}
        self._executors: dict[str, ActionExecutor] = {}
        self._verifiers: dict[str, ActionVerifier] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, bindings=len(self._bindings))

    def get_metrics(self) -> dict[str, int]:
        """Expose bounded registry counters without exposing routes or credentials."""
        return {
            "external_executor_registry_registered": len(self._bindings),
            "external_executor_registry_running": int(self._running),
        }
    def register(
        self,
        binding: ExternalExecutorBinding,
        executor: ActionExecutor,
        verifier: ActionVerifier,
    ) -> None:
        if not isinstance(binding, ExternalExecutorBinding):
            raise ValueError("binding must be ExternalExecutorBinding")
        if binding.executor_id in self._executors or binding.verifier_id in self._verifiers:
            raise ValueError("external executor or verifier ID already registered")
        self._bindings[binding.executor_id] = binding
        self._executors[binding.executor_id] = executor
        self._verifiers[binding.verifier_id] = verifier

    def executor_for(self, executor_id: str) -> ActionExecutor | None:
        return self._executors.get(str(executor_id))

    def verifier_for(self, verifier_id: str) -> ActionVerifier | None:
        return self._verifiers.get(str(verifier_id))

    def snapshot(self) -> dict[str, Any]:
        return {
            "registered": len(self._bindings),
            "bindings": [
                {
                    "executor_id": binding.executor_id,
                    "verifier_id": binding.verifier_id,
                    "feature_id": binding.feature_id,
                    "health_target_id": binding.health_target_id,
                }
                for binding in sorted(self._bindings.values(), key=lambda item: item.executor_id)
            ],
        }
