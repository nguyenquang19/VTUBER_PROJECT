"""Strict contracts for verified external action executors."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from interfaces.action_execution import ActionExecutor, ActionVerifier
from interfaces.base import Service
from interfaces.compatibility import ActionRequest, ActionResult


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    clean = value.strip()
    if clean != value:
        raise ValueError(f"{field_name} must be canonical")
    return clean


@dataclass(frozen=True)
class ExternalExecutorBinding:
    """One declared route; registration never grants permission or execution."""

    executor_id: str
    verifier_id: str
    feature_id: str
    health_target_id: str

    def __post_init__(self) -> None:
        for field_name in ("executor_id", "verifier_id", "feature_id", "health_target_id"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name),
            )


class RollbackStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RollbackResult:
    status: RollbackStatus
    reason_code: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "status", RollbackStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("rollback status is invalid") from exc
        object.__setattr__(
            self, "reason_code", _required_text(self.reason_code, "reason_code"),
        )
        if not isinstance(self.evidence_refs, tuple):
            raise ValueError("evidence_refs must be a tuple")
        object.__setattr__(self, "evidence_refs", tuple(
            _required_text(value, "evidence_ref") for value in self.evidence_refs
        ))


class ExternalActionExecutor(ActionExecutor):
    """External executor with an explicit compensating-action boundary."""

    @abstractmethod
    async def rollback(
        self, request: ActionRequest, result: ActionResult,
    ) -> RollbackResult:
        """Try a safe compensation without changing the original outcome."""


@dataclass(frozen=True)
class OBSSceneState:
    scene_name: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scene_name", _required_text(self.scene_name, "scene_name"))
        object.__setattr__(
            self, "evidence_ref", _required_text(self.evidence_ref, "evidence_ref"),
        )


@dataclass(frozen=True)
class OBSCommandAck:
    request_id: str
    accepted: bool
    evidence_ref: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required_text(self.request_id, "request_id"))
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be a bool")
        object.__setattr__(
            self, "evidence_ref", _required_text(self.evidence_ref, "evidence_ref"),
        )
        if self.error_code is not None:
            object.__setattr__(
                self, "error_code", _required_text(self.error_code, "error_code"),
            )
        if self.accepted and self.error_code is not None:
            raise ValueError("accepted acknowledgement cannot have an error_code")
        if not self.accepted and self.error_code is None:
            raise ValueError("rejected acknowledgement requires an error_code")


class OBSSceneTransportService(Service):
    """Minimal OBS WebSocket boundary used by the scene executor and verifier."""

    @abstractmethod
    async def get_current_program_scene(self) -> OBSSceneState:
        """Read the authoritative current program scene."""

    @abstractmethod
    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck:
        """Send one scene-switch command; acknowledgement is not verification."""


class ExternalExecutorRegistryService(Service):
    """Typed registry only; coordination and transaction ownership live elsewhere."""

    @abstractmethod
    def register(
        self,
        binding: ExternalExecutorBinding,
        executor: ExternalActionExecutor,
        verifier: ActionVerifier,
    ) -> None:
        """Register one declared typed route or reject a conflict."""

    @abstractmethod
    def executor_for(self, executor_id: str) -> ExternalActionExecutor | None:
        """Return a registered executor without invoking it."""

    @abstractmethod
    def verifier_for(self, verifier_id: str) -> ActionVerifier | None:
        """Return a registered verifier without invoking it."""

    @abstractmethod
    def binding_for(self, executor_id: str) -> ExternalExecutorBinding | None:
        """Return immutable route metadata for validation."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return an operator-safe registry summary."""
