"""Versioned structured trajectory contracts for Phase 14 observability."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Literal

from interfaces.action_execution import VerificationResult
from interfaces.base import Service
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.director_v2 import DirectorV2Context, DirectorV2Proposal


@dataclass(frozen=True)
class TrajectorySnapshotRefs:
    world_snapshot_id: str
    self_snapshot_id: str
    capability_snapshot_id: str

    def __post_init__(self) -> None:
        for name in (
            "world_snapshot_id", "self_snapshot_id", "capability_snapshot_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "world_snapshot_id": self.world_snapshot_id,
            "self_snapshot_id": self.self_snapshot_id,
            "capability_snapshot_id": self.capability_snapshot_id,
        }


@dataclass(frozen=True)
class TrajectoryReplayResult:
    trajectory_id: str
    matched: bool
    mismatches: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.trajectory_id, str) or not self.trajectory_id.strip():
            raise ValueError("trajectory_id must be a non-empty string")
        if not isinstance(self.matched, bool):
            raise ValueError("matched must be a bool")
        if not isinstance(self.mismatches, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.mismatches
        ):
            raise ValueError("mismatches must contain non-empty strings")
        if not isinstance(self.fingerprint, str) or not self.fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")


TrajectoryProposer = Callable[[DirectorV2Context], DirectorV2Proposal]


class TrajectoryRecordService(Service):
    """Observe Director decisions without owning selection or execution."""

    @abstractmethod
    def begin(self, context: DirectorV2Context, proposal: DirectorV2Proposal) -> str | None:
        """Open one bounded decision record, or return None when disabled."""

    @abstractmethod
    def mark_selection(
        self, trajectory_id: str, *, owner: Literal["legacy", "director_v2"],
    ) -> None:
        """Mark shadow-only or V2-owned selection without changing the decision."""

    @abstractmethod
    def record_action(self, trajectory_id: str, request: ActionRequest) -> None:
        """Attach one sanitized high-level action request."""

    @abstractmethod
    def record_result(
        self, trajectory_id: str, result: ActionResult,
        verification: VerificationResult,
    ) -> None:
        """Finalize an executed action with its verification outcome."""

    @abstractmethod
    def record_no_action(self, trajectory_id: str, *, reason_code: str) -> None:
        """Finalize an explicit WAIT without inventing an action request."""

    @abstractmethod
    def replay(
        self, trajectory_id: str, proposer: TrajectoryProposer,
    ) -> TrajectoryReplayResult:
        """Recompute only the deterministic proposal and compare structured output."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return a bounded sanitized operator projection."""
