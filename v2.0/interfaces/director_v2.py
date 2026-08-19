"""Typed Director V2 shadow and controlled-takeover contracts."""
from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import dataclass
from typing import Callable

from interfaces.base import Service


DIRECTOR_V2_SOURCES = (
    "chat", "thread", "goal", "world", "capability", "proactive", "wait",
)
DIRECTOR_V2_FAILURE_SOURCES = (
    "context", "world", "self", "capability", "transaction", "emergency",
    "operator", "chat", "goal", "thread", "proactive",
)


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

    def __post_init__(self) -> None:
        source = _required_string(self.source, "source")
        if source not in DIRECTOR_V2_SOURCES:
            raise ValueError("source is unsupported")
        object.__setattr__(self, "source", source)
        for name in ("candidate_id", "action_type", "capability_id"):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), name),
            )
        object.__setattr__(self, "score", _finite_number(self.score, "score"))
        object.__setattr__(
            self, "evidence_refs", _strict_strings(self.evidence_refs, "evidence_refs"),
        )
        if not isinstance(self.is_donation, bool):
            raise ValueError("is_donation must be a bool")


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
    source_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "created_at", _finite_number(self.created_at, "created_at", non_negative=True),
        )
        for name in (
            "world_snapshot_id", "self_snapshot_id", "capability_snapshot_id",
        ):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), name),
            )
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, DirectorV2Candidate) for item in self.candidates
        ):
            raise ValueError("candidates must be a tuple of DirectorV2Candidate")
        for name in (
            "emergency", "operator_hold", "safety_hold", "permission_hold",
            "transaction_conflict", "critical_state",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")
        failures = _strict_strings(self.source_failures, "source_failures")
        if any(item not in DIRECTOR_V2_FAILURE_SOURCES for item in failures):
            raise ValueError("source_failures contains an unsupported source")
        if len(set(failures)) != len(failures):
            raise ValueError("source_failures must be unique")
        object.__setattr__(self, "source_failures", failures)


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

    def __post_init__(self) -> None:
        for name in (
            "proposal_id", "action_type", "capability_id", "candidate_id",
        ):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), name),
            )
        object.__setattr__(
            self, "created_at", _finite_number(self.created_at, "created_at", non_negative=True),
        )
        reasons = _strict_strings(self.reason_codes, "reason_codes")
        if not reasons:
            raise ValueError("reason_codes must not be empty")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self, "evidence_refs", _strict_strings(self.evidence_refs, "evidence_refs"),
        )
        object.__setattr__(self, "score", _finite_number(self.score, "score"))


@dataclass(frozen=True)
class DirectorV2TakeoverSelection:
    """Agreement result; the legacy decision remains the executable object."""

    accepted: bool
    stage: str
    reason_code: str
    action_type: str
    proposal_id: str


DirectorV2ContextProvider = Callable[[], DirectorV2Context]


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strict_strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    result = tuple(_required_string(item, field_name) for item in value)
    return result


def _finite_number(
    value: object, field_name: str, *, non_negative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (non_negative and result < 0):
        raise ValueError(f"{field_name} must be a finite number")
    return result


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
