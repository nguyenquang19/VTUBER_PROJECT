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
