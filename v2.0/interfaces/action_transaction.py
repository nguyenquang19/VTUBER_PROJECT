"""Contract for delivery-aware Director action transactions (M10.1)."""
from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from interfaces.base import Service


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
    def reserve(self, action: str, idempotency_key: str) -> ReservationResult:
        """Reserve an action or return its already-committed transaction."""

    @abstractmethod
    def mark_generated(self, transaction_id: str) -> ActionTransaction:
        """Record that generation/filtering produced a deliverable output."""

    @abstractmethod
    def mark_delivering(self, transaction_id: str) -> ActionTransaction:
        """Record entry into the external delivery boundary."""

    @abstractmethod
    def mark_delivered(self, transaction_id: str) -> ActionTransaction:
        """Record successful delivery according to the active output policy."""

    @abstractmethod
    def commit(self, transaction_id: str) -> ActionTransaction:
        """Commit application side effects after delivery success."""

    @abstractmethod
    def release(self, transaction_id: str, reason: str) -> ActionTransaction:
        """Release a failed reservation without committing side effects."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return a bounded operator-safe transaction snapshot."""
