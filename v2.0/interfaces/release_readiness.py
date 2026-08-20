"""Strict Phase 15 release-readiness and closed-loop canary contracts."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from interfaces.action_execution import ActionRequest
from interfaces.base import Service
from interfaces.trajectory import TrajectorySnapshotRefs


@dataclass(frozen=True)
class ClosedLoopCanaryRecord:
    schema_version: int
    canary_id: str
    source_revision: str
    current_product_version: str
    target_product_version: str
    started_at: datetime
    completed_at: datetime
    action_id: str
    proposal_id: str
    action_type: str
    capability_id: str
    pre_snapshot: TrajectorySnapshotRefs
    post_snapshot: TrajectorySnapshotRefs
    result_status: str
    verified: bool
    verification_source: str
    transaction_committed: bool
    world_projected: bool
    capability_rechecked: bool
    next_proposal_id: str
    next_action_type: str
    outcome: str
    reason_code: str
    rollback_outcome: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
        for name in (
            "canary_id", "source_revision", "current_product_version",
            "target_product_version", "action_id", "proposal_id", "action_type",
            "capability_id", "result_status", "verification_source",
            "next_proposal_id", "next_action_type", "outcome", "reason_code",
            "rollback_outcome",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if len(self.source_revision) != 40 or any(
            char not in "0123456789abcdef" for char in self.source_revision
        ):
            raise ValueError("source_revision must be a lowercase full Git SHA")
        for name in ("started_at", "completed_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        for name in (
            "verified", "transaction_committed", "world_projected",
            "capability_rechecked",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a bool")
        if not isinstance(self.pre_snapshot, TrajectorySnapshotRefs) or not isinstance(
            self.post_snapshot, TrajectorySnapshotRefs,
        ):
            raise ValueError("canary snapshots must be TrajectorySnapshotRefs")

    @property
    def passed(self) -> bool:
        return (
            self.outcome == "passed"
            and self.verified
            and self.transaction_committed
            and self.world_projected
            and self.capability_rechecked
            and self.pre_snapshot.world_snapshot_id
            != self.post_snapshot.world_snapshot_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "marker": "mai_closed_loop_canary",
            "sanitized": True,
            "source_revision": self.source_revision,
            "current_product_version": self.current_product_version,
            "target_product_version": self.target_product_version,
            "generated_at_utc": self.completed_at.isoformat(),
            "canary_id": self.canary_id,
            "started_at_utc": self.started_at.isoformat(),
            "action": {
                "action_id": self.action_id,
                "proposal_id": self.proposal_id,
                "action_type": self.action_type,
                "capability_id": self.capability_id,
            },
            "pre_snapshot": self.pre_snapshot.to_dict(),
            "post_snapshot": self.post_snapshot.to_dict(),
            "result": {
                "status": self.result_status,
                "verified": self.verified,
                "verification_source": self.verification_source,
                "transaction_committed": self.transaction_committed,
                "world_projected": self.world_projected,
                "rollback_outcome": self.rollback_outcome,
            },
            "next_decision": {
                "proposal_id": self.next_proposal_id,
                "action_type": self.next_action_type,
                "capability_rechecked": self.capability_rechecked,
            },
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "passed": self.passed,
        }


class ClosedLoopCanaryService(Service):
    """Run one explicit canary without becoming an autonomous action selector."""

    @abstractmethod
    async def run(self, request: ActionRequest) -> ClosedLoopCanaryRecord:
        """Run one typed operator-requested loop and return sanitized evidence."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded status and recent records without action argument values."""
