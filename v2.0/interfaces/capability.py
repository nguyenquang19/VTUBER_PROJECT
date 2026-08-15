"""Read-only capability registry contract for Phase 4."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus, Service
from interfaces.compatibility import Capability, CapabilityAvailability


CapabilityHealthProvider = Callable[[], HealthStatus | Mapping[str, Any] | bool]


class CapabilityRegistryService(Service):
    """Report declared capability availability without executing actions."""

    @abstractmethod
    def capability(self, capability_id: str) -> Capability | None:
        """Return one immutable declaration, never an executable callable."""

    @abstractmethod
    def availability(self, capability_id: str) -> CapabilityAvailability:
        """Return the deterministic current status for one declared capability."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return the bounded, operator-safe registry projection."""

    @abstractmethod
    def register_verifier(self, verifier_id: str) -> None:
        """Register one authoritative verifier identity for availability only."""

    @abstractmethod
    def register_health_provider(
        self, executor_id: str, provider: CapabilityHealthProvider,
    ) -> None:
        """Register a synchronous public health provider for one executor ID."""
