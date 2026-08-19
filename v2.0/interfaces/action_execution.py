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

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be a bool")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("reason_code must be a non-empty string")
        if not isinstance(self.evidence_refs, tuple):
            raise ValueError("evidence_refs must be a tuple")
        if not all(isinstance(item, str) and item.strip() for item in self.evidence_refs):
            raise ValueError("evidence_refs must contain non-empty strings")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "reason_code", self.reason_code.strip())
        object.__setattr__(
            self, "evidence_refs", tuple(item.strip() for item in self.evidence_refs),
        )


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


class LocalActionBoundaryService(Service):
    """Verify an existing local side effect without owning business commit state."""

    @abstractmethod
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Execute and verify one idempotent local action request."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return a bounded summary without text, audio or credentials."""
