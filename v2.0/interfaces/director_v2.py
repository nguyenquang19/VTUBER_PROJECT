"""Typed Director V2 shadow and controlled-takeover contracts."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Callable

from interfaces.base import Service


@dataclass(frozen=True)
class DirectorV2Candidate:
    """A bounded candidate; it is never an executable command."""

    source: str
    candidate_id: str
    action_type: str
    capability_id: str
    score: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    is_donation: bool = False


@dataclass(frozen=True)
class DirectorV2Context:
    """Read-only inputs supplied by the composition root at one shadow tick."""

    created_at: float
    world_snapshot_id: str
    self_snapshot_id: str
    capability_snapshot_id: str
    candidates: tuple[DirectorV2Candidate, ...] = ()
    emergency: bool = False
    operator_hold: bool = False
    safety_hold: bool = False
    permission_hold: bool = False
    transaction_conflict: bool = False
    critical_state: bool = False


@dataclass(frozen=True)
class DirectorV2Proposal:
    """Deterministic shadow output, safe to retain and replay."""

    proposal_id: str
    created_at: float
    action_type: str
    capability_id: str
    candidate_id: str
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class DirectorV2TakeoverSelection:
    """Agreement result; the legacy decision remains the executable object."""

    accepted: bool
    stage: str
    reason_code: str
    action_type: str
    proposal_id: str


DirectorV2ContextProvider = Callable[[], DirectorV2Context]


class DirectorV2ShadowService(Service):
    """Observe, propose and retain bounded records without changing live behavior."""

    @abstractmethod
    def propose(self, context: DirectorV2Context) -> DirectorV2Proposal:
        """Return one deterministic proposal without mutating context owners."""

    @abstractmethod
    def propose_current(self) -> DirectorV2Proposal:
        """Propose from the composition-root context provider without side effects."""

    @abstractmethod
    def snapshot(self) -> dict[str, object]:
        """Return the bounded operator-safe shadow projection."""


class DirectorV2TakeoverService(Service):
    """Feature-gated agreement selector; it must never execute a turn."""

    @abstractmethod
    def evaluate(
        self, *, legacy_action: str, proposal: DirectorV2Proposal | None,
        evidence_ids: tuple[str, ...] = (),
    ) -> DirectorV2TakeoverSelection:
        """Return an agreement result without altering the legacy decision."""

    @abstractmethod
    def snapshot(self) -> dict[str, object]:
        """Return bounded takeover acceptance/fallback records."""
