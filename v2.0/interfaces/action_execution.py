"""Typed general-action execution contracts for the Phase 5 mock loop."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from interfaces.base import Service
from interfaces.compatibility import ActionRequest, ActionResult


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    source: str | None
    reason_code: str
    evidence_refs: tuple[str, ...] = ()


class ActionExecutor(Service):
    """Execute one typed request without committing application state."""

    @abstractmethod
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Return an attempt result; it is not success until verification."""


class ActionVerifier(Service):
    """Read an authoritative outcome independently from an executor claim."""

    @abstractmethod
    async def verify(
        self, request: ActionRequest, result: ActionResult,
    ) -> VerificationResult:
        """Return whether the external/mock authority confirms the request."""


class GeneralActionService(Service):
    """Coordinate validation, transaction, execution, verification and rollback."""

    @abstractmethod
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Run one bounded closed loop or return a non-verified result."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded, operator-safe action outcomes."""
