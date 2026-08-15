"""Typed, inert registry contract for future external action executors."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from interfaces.action_execution import ActionExecutor, ActionVerifier
from interfaces.base import Service


@dataclass(frozen=True)
class ExternalExecutorBinding:
    """One future external route; registration never grants permission or execution."""

    executor_id: str
    verifier_id: str
    feature_id: str
    health_target_id: str

    def __post_init__(self) -> None:
        for field_name in ("executor_id", "verifier_id", "feature_id", "health_target_id"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")


class ExternalExecutorRegistryService(Service):
    """Registry-only service; it must never execute or commit a request itself."""

    @abstractmethod
    def register(
        self,
        binding: ExternalExecutorBinding,
        executor: ActionExecutor,
        verifier: ActionVerifier,
    ) -> None:
        """Register one fully typed future route or reject conflicting IDs."""

    @abstractmethod
    def executor_for(self, executor_id: str) -> ActionExecutor | None:
        """Return a registered executor without invoking it."""

    @abstractmethod
    def verifier_for(self, verifier_id: str) -> ActionVerifier | None:
        """Return a registered verifier without invoking it."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return an operator-safe registry summary."""
