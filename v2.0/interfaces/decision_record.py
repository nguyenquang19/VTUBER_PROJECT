"""Versioned operator view model for Director decisions (M10.3)."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from interfaces.base import Service


class DecisionCandidateSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_count: int = 0
    pool_size: int = 0
    pulse_state: str = "normal"
    active_goal_id: str | None = None
    safety_hold: bool = False
    candidate_kinds: tuple[str, ...] = ()
    top_score: float | None = None


class DecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    decision_id: str
    created_at: float
    updated_at: float
    action: str
    reason: str
    segment: str
    evidence_refs: tuple[str, ...] = ()
    candidate_summary: DecisionCandidateSummary
    hard_rejection_reason: str = ""
    transaction_id: str | None = None
    transaction_state: str | None = None
    delivery_state: str = "not_started"
    outcome: str = "selected"


class DecisionRecordService(Service):
    @abstractmethod
    def record_decision(
        self,
        *,
        created_at: float,
        action: str,
        reason: str,
        segment: str,
        evidence_refs: tuple[str, ...],
        candidate_summary: DecisionCandidateSummary,
        hard_rejection_reason: str = "",
    ) -> DecisionRecord | None:
        """Create one bounded, privacy-safe decision record."""

    @abstractmethod
    def update_transaction(
        self,
        decision_id: str,
        *,
        transaction_id: str,
        transaction_state: str,
        delivery_state: str,
        outcome: str,
    ) -> DecisionRecord | None:
        """Attach the latest delivery-aware transaction result."""

    @abstractmethod
    def update_outcome(
        self, decision_id: str, *, delivery_state: str, outcome: str,
    ) -> DecisionRecord | None:
        """Finalize delivery when action transactions are disabled."""

    @abstractmethod
    def classify_hard_rejection(self, action: str, reason: str) -> str:
        """Return the configured rejection reason, or an empty string."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return the versioned operator view model."""
