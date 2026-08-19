"""Bounded typed registry for real external executor routes."""
from __future__ import annotations

from typing import Any

from interfaces.action_execution import ActionVerifier
from interfaces.base import HealthStatus
from interfaces.external_executor import (
    ExternalActionExecutor,
    ExternalExecutorBinding,
    ExternalExecutorRegistryService,
)


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    return value


class ExternalExecutorRegistry(ExternalExecutorRegistryService):
    """Own route lifecycle without granting permission or coordinating actions."""

    service_id = "external_executor_registry"

    def __init__(
        self,
        max_bindings: int = 8,
        *,
        allowed_bindings: tuple[ExternalExecutorBinding, ...] | None = None,
    ) -> None:
        if isinstance(max_bindings, bool) or not isinstance(max_bindings, int) or max_bindings <= 0:
            raise ValueError("max_bindings must be a positive int")
        if allowed_bindings is not None:
            if not isinstance(allowed_bindings, tuple) or not all(
                isinstance(item, ExternalExecutorBinding) for item in allowed_bindings
            ):
                raise ValueError("allowed_bindings must contain typed bindings")
            if len(set(allowed_bindings)) != len(allowed_bindings):
                raise ValueError("allowed_bindings must be unique")
        self._max_bindings = max_bindings
        self._allowed_bindings = (
            frozenset(allowed_bindings) if allowed_bindings is not None else None
        )
        self._bindings: dict[str, ExternalExecutorBinding] = {}
        self._executors: dict[str, ExternalActionExecutor] = {}
        self._verifiers: dict[str, ActionVerifier] = {}
        self._running = False
        self._started_services: list[Any] = []
        self._lifecycle_errors = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        started: list[Any] = []
        try:
            for service in self._services():
                await service.start()
                started.append(service)
        except Exception:
            self._lifecycle_errors += 1
            for service in reversed(started):
                try:
                    await service.stop()
                except Exception:
                    self._lifecycle_errors += 1
            self._running = False
            raise
        self._started_services = started

    async def stop(self) -> None:
        if not self._running and not self._started_services:
            return
        self._running = False
        services, self._started_services = self._started_services, []
        for service in reversed(services):
            try:
                await service.stop()
            except Exception:
                self._lifecycle_errors += 1

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._bindings:
            return HealthStatus.degraded(self.service_id, "no external routes registered")
        unhealthy: list[str] = []
        for service in self._services():
            try:
                status = await service.health_check()
            except Exception:
                unhealthy.append(service.service_id)
                continue
            if not status.is_ok:
                unhealthy.append(service.service_id)
        if unhealthy:
            return HealthStatus.degraded(
                self.service_id, "external route degraded", targets=sorted(unhealthy),
            )
        return HealthStatus.healthy(self.service_id, bindings=len(self._bindings))

    def get_metrics(self) -> dict[str, int]:
        return {
            "external_executor_registry_registered": len(self._bindings),
            "external_executor_registry_capacity": self._max_bindings,
            "external_executor_registry_running": int(self._running),
            "external_executor_registry_lifecycle_errors_total": self._lifecycle_errors,
        }

    def register(
        self,
        binding: ExternalExecutorBinding,
        executor: ExternalActionExecutor,
        verifier: ActionVerifier,
    ) -> None:
        if self._running:
            raise ValueError("cannot register external route while running")
        if not isinstance(binding, ExternalExecutorBinding):
            raise ValueError("binding must be ExternalExecutorBinding")
        if not isinstance(executor, ExternalActionExecutor):
            raise ValueError("executor must implement ExternalActionExecutor")
        if not isinstance(verifier, ActionVerifier):
            raise ValueError("verifier must implement ActionVerifier")
        if self._allowed_bindings is not None and binding not in self._allowed_bindings:
            raise ValueError("external binding is not declared")
        current_executor = self._executors.get(binding.executor_id)
        current_verifier = self._verifiers.get(binding.verifier_id)
        current_binding = self._bindings.get(binding.executor_id)
        if (
            current_binding == binding
            and current_executor is executor
            and current_verifier is verifier
        ):
            return
        if current_executor is not None or current_verifier is not None:
            raise ValueError("external executor or verifier ID already registered")
        if len(self._bindings) >= self._max_bindings:
            raise ValueError("external executor registry capacity exceeded")
        self._bindings[binding.executor_id] = binding
        self._executors[binding.executor_id] = executor
        self._verifiers[binding.verifier_id] = verifier

    def executor_for(self, executor_id: str) -> ExternalActionExecutor | None:
        value = _identifier(executor_id)
        return self._executors.get(value) if value is not None else None

    def verifier_for(self, verifier_id: str) -> ActionVerifier | None:
        value = _identifier(verifier_id)
        return self._verifiers.get(value) if value is not None else None

    def binding_for(self, executor_id: str) -> ExternalExecutorBinding | None:
        value = _identifier(executor_id)
        return self._bindings.get(value) if value is not None else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "registered": len(self._bindings),
            "capacity": self._max_bindings,
            "running": self._running,
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

    def _services(self) -> tuple[Any, ...]:
        ordered: list[Any] = []
        seen: set[int] = set()
        for service in (*self._executors.values(), *self._verifiers.values()):
            if id(service) not in seen:
                ordered.append(service)
                seen.add(id(service))
        return tuple(ordered)
