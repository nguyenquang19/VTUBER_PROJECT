"""Canonical S5 execution, verification, transaction and outcome contracts."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

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


class ActionTransactionState(str, Enum):
    RESERVED = "reserved"
    GENERATED = "generated"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    COMMITTED = "committed"
    RELEASED = "released"


class ActionTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_id: str
    idempotency_key: str
    action: str
    state: ActionTransactionState
    created_at: float
    updated_at: float
    reason: str = ""


class ReservationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction: ActionTransaction
    created: bool


class ActionTransactionService(Service):
    @abstractmethod
    def get(self, transaction_id: str) -> ActionTransaction | None: ...
    @abstractmethod
    def find_by_idempotency_key(self, idempotency_key: str) -> ActionTransaction | None: ...
    @abstractmethod
    def reserve(self, action: str, idempotency_key: str) -> ReservationResult: ...
    @abstractmethod
    def mark_generated(self, transaction_id: str) -> ActionTransaction: ...
    @abstractmethod
    def mark_delivering(self, transaction_id: str) -> ActionTransaction: ...
    @abstractmethod
    def mark_delivered(self, transaction_id: str) -> ActionTransaction: ...
    @abstractmethod
    def commit(self, transaction_id: str) -> ActionTransaction: ...
    @abstractmethod
    def release(self, transaction_id: str, reason: str) -> ActionTransaction: ...
    @abstractmethod
    def snapshot(self) -> dict[str, Any]: ...


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must be canonical text")
    return value


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ExternalExecutorBinding:
    executor_id: str
    verifier_id: str
    feature_id: str
    health_target_id: str

    def __post_init__(self) -> None:
        for name in ("executor_id", "verifier_id", "feature_id", "health_target_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))


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
        object.__setattr__(self, "status", RollbackStatus(self.status))
        object.__setattr__(self, "reason_code", _required_text(self.reason_code, "reason_code"))
        if not isinstance(self.evidence_refs, tuple):
            raise ValueError("evidence_refs must be a tuple")
        object.__setattr__(self, "evidence_refs", tuple(
            _required_text(value, "evidence_ref") for value in self.evidence_refs
        ))


class ExternalActionExecutor(ActionExecutor):
    @abstractmethod
    async def rollback(self, request: ActionRequest, result: ActionResult) -> RollbackResult: ...


@dataclass(frozen=True)
class OBSSceneState:
    scene_name: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scene_name", _required_text(self.scene_name, "scene_name"))
        object.__setattr__(self, "evidence_ref", _required_text(self.evidence_ref, "evidence_ref"))


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
        object.__setattr__(self, "evidence_ref", _required_text(self.evidence_ref, "evidence_ref"))
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _required_text(self.error_code, "error_code"))
        if self.accepted == (self.error_code is not None):
            raise ValueError("accepted acknowledgement cannot have an error; rejected requires one")


class OBSSceneTransportService(Service):
    @abstractmethod
    async def get_current_program_scene(self) -> OBSSceneState: ...
    @abstractmethod
    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck: ...


class ExternalExecutorRegistryService(Service):
    @abstractmethod
    def register(self, binding: ExternalExecutorBinding, executor: ExternalActionExecutor, verifier: ActionVerifier) -> None: ...
    @abstractmethod
    def executor_for(self, executor_id: str) -> ExternalActionExecutor | None: ...
    @abstractmethod
    def verifier_for(self, verifier_id: str) -> ActionVerifier | None: ...
    @abstractmethod
    def binding_for(self, executor_id: str) -> ExternalExecutorBinding | None: ...
    @abstractmethod
    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutionReservation:
    schema_version: int
    execution_id: str
    transaction_id: str
    action_type: str
    idempotency_key: str
    created: bool
    reserved_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for name in ("execution_id", "transaction_id", "action_type", "idempotency_key"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.created, bool):
            raise ValueError("created must be a bool")
        object.__setattr__(self, "reserved_at", _utc(self.reserved_at, "reserved_at"))


@dataclass(frozen=True)
class VerifiedExecution:
    schema_version: int
    execution_id: str
    transaction_id: str
    request: ActionRequest
    result: ActionResult
    verification: VerificationResult
    verified_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for name in ("execution_id", "transaction_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.request, ActionRequest) or not isinstance(self.result, ActionResult):
            raise ValueError("request and result must be typed")
        if not isinstance(self.verification, VerificationResult):
            raise ValueError("verification must be VerificationResult")
        if self.result.action_id != self.request.action_id or not self.result.verified or not self.verification.verified or self.result.verification_source != self.verification.source:
            raise ValueError("verified execution requires matching authoritative success")
        object.__setattr__(self, "verified_at", _utc(self.verified_at, "verified_at"))


class OutcomeDisposition(str, Enum):
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    DUPLICATE_COMMITTED = "DUPLICATE_COMMITTED"


@dataclass(frozen=True)
class OutcomeCommit:
    schema_version: int
    outcome_ref: str
    execution_id: str
    transaction_id: str
    disposition: OutcomeDisposition
    reason_code: str
    evidence_refs: tuple[str, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for name in ("outcome_ref", "execution_id", "transaction_id", "reason_code"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "disposition", OutcomeDisposition(self.disposition))
        if not isinstance(self.evidence_refs, tuple):
            raise ValueError("evidence_refs must be a tuple")
        object.__setattr__(self, "evidence_refs", tuple(_required_text(value, "evidence_ref") for value in self.evidence_refs))
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))


class ExecutionBoundaryService(Service):
    @abstractmethod
    def reserve(self, action_type: str, idempotency_key: str) -> ExecutionReservation: ...
    @abstractmethod
    async def execute_verified(self, reservation: ExecutionReservation, request: ActionRequest) -> VerifiedExecution | None: ...


class OutcomeCommitterService(Service):
    @abstractmethod
    def commit_verified(self, reservation: ExecutionReservation, verified: VerifiedExecution) -> OutcomeCommit: ...
    @abstractmethod
    def release(self, reservation: ExecutionReservation, reason_code: str) -> OutcomeCommit: ...


@dataclass(frozen=True)
class ExecutionConfig:
    schema_version: int
    transactions: Mapping[str, Any]
    local: Mapping[str, Any]
    external: Mapping[str, Any]

    _KEYS = frozenset({"schema_version", "transactions", "local", "external"})
    _TRANSACTION_KEYS = frozenset({
        "max_recent", "max_recent_outcomes", "max_reason_chars", "max_evidence_refs",
    })
    _LOCAL_KEYS = frozenset({
        "execution_timeout_s", "max_idempotency_records", "max_evidence_refs",
    })
    _EXTERNAL_KEYS = frozenset({
        "execution_timeout_s", "verification_timeout_s", "rollback_timeout_s",
        "max_recent_results", "max_idempotency_records",
        "max_verification_evidence_refs", "max_registry_bindings", "obs",
    })
    _OBS_KEYS = frozenset({
        "host", "port", "use_tls", "password_env", "connect_timeout_s",
        "request_timeout_s", "health_timeout_s", "health_ttl_s", "max_attempts",
        "retry_backoff_s", "max_scene_name_chars", "max_authority_records",
        "max_evidence_refs", "max_message_bytes",
    })

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        for name, keys in (
            ("transactions", self._TRANSACTION_KEYS),
            ("local", self._LOCAL_KEYS),
            ("external", self._EXTERNAL_KEYS),
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or set(value) != keys:
                raise ValueError(f"{name} config keys mismatch")
        for name in self._TRANSACTION_KEYS:
            value = self.transactions[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"transactions.{name} must be a positive integer")
        for name in ("max_idempotency_records", "max_evidence_refs"):
            value = self.local[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"local.{name} must be a positive integer")
        timeout = self.local["execution_timeout_s"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("local.execution_timeout_s must be positive")
        for name in (
            "max_recent_results", "max_idempotency_records",
            "max_verification_evidence_refs", "max_registry_bindings",
        ):
            value = self.external[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"external.{name} must be a positive integer")
        for name in ("execution_timeout_s", "verification_timeout_s", "rollback_timeout_s"):
            value = self.external[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"external.{name} must be positive")
        obs = self.external["obs"]
        if not isinstance(obs, Mapping) or set(obs) != self._OBS_KEYS:
            raise ValueError("external.obs config keys mismatch")
        for name in ("host", "password_env"):
            _required_text(obs[name], f"external.obs.{name}")
        port = obs["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("external.obs.port must be in 1..65535")
        if not isinstance(obs["use_tls"], bool):
            raise ValueError("external.obs.use_tls must be a bool")
        for name in (
            "connect_timeout_s", "request_timeout_s", "health_timeout_s", "health_ttl_s",
        ):
            value = obs[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"external.obs.{name} must be positive")
        retry = obs["retry_backoff_s"]
        if isinstance(retry, bool) or not isinstance(retry, (int, float)) or retry < 0:
            raise ValueError("external.obs.retry_backoff_s must be non-negative")
        for name in (
            "max_attempts", "max_scene_name_chars", "max_authority_records",
            "max_evidence_refs", "max_message_bytes",
        ):
            value = obs[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"external.obs.{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionConfig":
        if not isinstance(value, Mapping) or set(value) != cls._KEYS:
            raise ValueError("execution config keys mismatch")
        return cls(
            schema_version=value["schema_version"],
            transactions=dict(value["transactions"]),
            local=dict(value["local"]),
            external=dict(value["external"]),
        )
