"""Interfaces for M9 live-operations services."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from interfaces.base import Service


HealthCheck = Callable[[], Awaitable[Any]]
RecoveryAction = Callable[[], Awaitable[None]]


class TurnJournalStage(str, Enum):
    """Sanitized observable milestones; never a decision or commit authority."""

    EVENT_RECEIVED = "EVENT_RECEIVED"
    OPPORTUNITY_OPENED = "OPPORTUNITY_OPENED"
    DECISION_RECORDED = "DECISION_RECORDED"
    GENERATION_STARTED = "GENERATION_STARTED"
    GENERATION_COMPLETED = "GENERATION_COMPLETED"
    DELIVERY_RESERVED = "DELIVERY_RESERVED"
    DELIVERY_STARTED = "DELIVERY_STARTED"
    DELIVERY_FINISHED = "DELIVERY_FINISHED"
    OUTCOME_COMMITTED = "OUTCOME_COMMITTED"
    OUTCOME_RELEASED = "OUTCOME_RELEASED"
    CONTINUITY_COMMITTED = "CONTINUITY_COMMITTED"


@dataclass(frozen=True)
class TurnJournalEvent:
    """One immutable, privacy-safe patch to a turn lineage."""

    schema_version: int
    lineage_id: str
    stage: TurnJournalStage
    occurred_at: datetime
    monotonic_ns: int | None = None
    session_id: str | None = None
    event_id: str | None = None
    opportunity_id: str | None = None
    decision_id: str | None = None
    attempt_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    transaction_id: str | None = None
    outcome_ref: str | None = None
    continuity_id: str | None = None
    action_id: str | None = None
    owner: str | None = None
    mode: str | None = None
    terminal_state: str | None = None
    verified: bool | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    projection_kind: str | None = None
    projection_json: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("turn journal event schema_version must be 1")
        if not isinstance(self.lineage_id, str) or not self.lineage_id.strip():
            raise ValueError("turn journal lineage_id must be non-empty")
        object.__setattr__(self, "lineage_id", self.lineage_id.strip())
        if not isinstance(self.stage, TurnJournalStage):
            raise ValueError("turn journal stage is invalid")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ValueError("turn journal occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))
        if self.monotonic_ns is not None and (
            isinstance(self.monotonic_ns, bool)
            or not isinstance(self.monotonic_ns, int)
            or self.monotonic_ns < 0
        ):
            raise ValueError("turn journal monotonic_ns must be non-negative")
        if self.verified is not None and type(self.verified) is not bool:
            raise ValueError("turn journal verified must be bool or None")
        for field_name in (
            "session_id", "event_id", "opportunity_id", "decision_id",
            "attempt_id", "turn_id", "request_id", "transaction_id",
            "outcome_ref", "continuity_id", "action_id", "owner", "mode",
            "terminal_state", "projection_kind",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"turn journal {field_name} must be non-empty")
                object.__setattr__(self, field_name, value.strip())
        for field_name in ("reason_codes", "evidence_refs"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"turn journal {field_name} must be non-empty strings")
            normalized = tuple(value.strip() for value in values)
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"turn journal {field_name} must be unique")
            object.__setattr__(self, field_name, normalized)
        if self.projection_json is not None and (
            not isinstance(self.projection_json, str) or not self.projection_json.strip()
        ):
            raise ValueError("turn journal projection_json must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lineage_id": self.lineage_id,
            "stage": self.stage.value,
            "occurred_at": self.occurred_at.isoformat(),
            "monotonic_ns": self.monotonic_ns,
            **{
                name: getattr(self, name)
                for name in (
                    "session_id", "event_id", "opportunity_id", "decision_id",
                    "attempt_id", "turn_id", "request_id", "transaction_id",
                    "outcome_ref", "continuity_id", "action_id", "owner", "mode",
                    "terminal_state", "verified", "projection_kind",
                )
            },
            "reason_codes": list(self.reason_codes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class TurnLineageRecord:
    """Bounded materialized view of every known stage for one turn."""

    schema_version: int
    lineage_id: str
    created_at: datetime
    updated_at: datetime
    events: tuple[TurnJournalEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for event in self.events:
            for key, value in event.to_dict().items():
                if key not in {"schema_version", "lineage_id", "stage", "occurred_at"} and value not in (
                    None, [], "",
                ):
                    latest[key] = value
        return {
            "schema_version": self.schema_version,
            "lineage_id": self.lineage_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "latest": latest,
            "stages": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class OperationsSnapshot:
    schema_version: int
    generated_at: datetime
    sections: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations_schema_version": self.schema_version,
            "operations_generated_at": self.generated_at.astimezone(timezone.utc).isoformat(),
            **dict(self.sections),
        }


@dataclass(frozen=True)
class OperationsCommand:
    command_id: str
    name: str
    issued_at: datetime
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class OperationsCommandResult:
    command_id: str
    accepted: bool
    status_code: int
    payload: Mapping[str, Any]


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


class DashboardDataSourceService(OperationsSnapshotService):
    """Independent dashboard source for live snapshots, history and safe commands."""

    @abstractmethod
    async def snapshot_for(self, source_mode: str) -> dict[str, Any]:
        """Read one snapshot for auto, live or history mode."""

    @abstractmethod
    async def query_history(
        self,
        *,
        session_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        kind: str | None = None,
        delivered: bool | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return one bounded read-only projection of turn and delivery journals."""

    @abstractmethod
    async def forward_command(
        self, path: str, payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Forward one allowlisted command to the loopback live dashboard."""


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


class SoakMonitorService(Service):
    @abstractmethod
    async def run(self, duration_s: float | None = None) -> dict[str, Any]:
        """Run one controlled soak and return its gate report."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded live soak progress and measurements."""


class IncidentLogService(Service):
    @abstractmethod
    def record_incident(
        self, *, severity: str, component: str, summary: str,
        action: str, status: str = "open", evidence_refs: list[str] | None = None,
    ) -> str:
        """Append one sanitized, versioned incident and return its identifier."""

    @abstractmethod
    def resolve(self, incident_id: str, resolution: str) -> bool:
        """Append a resolution event for an existing incident."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded recent incidents and unresolved counts."""


class TurnJournalService(Service):
    @abstractmethod
    def append(self, event: TurnJournalEvent) -> TurnLineageRecord:
        """Append one idempotent sanitized stage without changing runtime authority."""

    @abstractmethod
    def get(self, lineage_id: str) -> TurnLineageRecord | None:
        """Read one bounded lineage by canonical identifier."""

    @abstractmethod
    def recent(self, limit: int | None = None) -> tuple[TurnLineageRecord, ...]:
        """Read recent bounded lineages in oldest-to-newest order."""

    @abstractmethod
    def projection(self, lineage_id: str, projection_kind: str) -> str | None:
        """Read the latest sanitized compatibility projection for one lineage."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded recent lineages and journal counters."""


class OperationsSurfaceService(Service):
    @abstractmethod
    async def snapshot_section(self, name: str) -> Any:
        """Read one failure-isolated operator section without collecting all providers."""

    @abstractmethod
    async def snapshot(self) -> OperationsSnapshot:
        """Read one failure-isolated operator snapshot."""

    @abstractmethod
    async def execute(self, command: OperationsCommand) -> OperationsCommandResult:
        """Dispatch one explicitly registered operator command."""

    @abstractmethod
    def prometheus_text(self) -> bytes:
        """Expose canonical metrics without granting dashboard policy authority."""
