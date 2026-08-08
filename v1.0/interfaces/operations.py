"""Interfaces for M9 live-operations services."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Awaitable, Callable

from interfaces.base import Service


HealthCheck = Callable[[], Awaitable[Any]]
RecoveryAction = Callable[[], Awaitable[None]]


class HealthSupervisorService(Service):
    @abstractmethod
    def register_target(
        self,
        service_id: str,
        check: HealthCheck,
        restart: RecoveryAction | None = None,
    ) -> None:
        """Register a health probe and optional bounded recovery action."""

    @abstractmethod
    def pause_recovery(self, reason: str) -> None:
        """Block automatic recovery during shutdown or emergency stop."""

    @abstractmethod
    def resume_recovery(self) -> None:
        """Allow bounded recovery again without resetting open circuits."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return operator-safe target state and recovery counters."""


class ShutdownCoordinatorService(Service):
    @abstractmethod
    def register_step(self, name: str, callback: RecoveryAction) -> None:
        """Append an ordered, bounded shutdown callback."""

    @abstractmethod
    async def shutdown(self) -> dict[str, Any]:
        """Run shutdown once and return the durable shutdown report."""


class OperatorControlService(Service):
    @abstractmethod
    async def pause(self, reason: str) -> bool:
        """Pause agent action production while keeping observability online."""

    @abstractmethod
    async def resume(self, reason: str) -> bool:
        """Resume action production after an operator pause."""

    @abstractmethod
    def record_operator_action(self, action: str, target: str, outcome: str) -> None:
        """Append a privacy-safe operator audit event."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return pause state, action queue, and recent audit entries."""


class OperationsSnapshotService(Service):
    @abstractmethod
    async def snapshot(self) -> dict[str, Any]:
        """Read the latest durable operations snapshot asynchronously."""


class EmergencyControlService(Service):
    @abstractmethod
    async def trigger(self, reason: str = "emergency stop") -> bool:
        """Latch action output off and cancel in-flight side effects."""

    @abstractmethod
    async def resume(self, reason: str = "operator resume") -> bool:
        """Prune stale work and reopen action output."""

    @abstractmethod
    def permits_speech(self) -> bool:
        """Return whether new speech may cross the output boundary."""

    @abstractmethod
    def permits_environment_action(self) -> bool:
        """Return whether a future environment action may execute."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return the latched state and counters."""
