"""S5 compatibility re-export; canonical contracts live in interfaces.execution."""
from interfaces.execution import (
    ActionTransaction,
    ActionTransactionService,
    ActionTransactionState,
    ReservationResult,
)

__all__ = ["ActionTransaction", "ActionTransactionService", "ActionTransactionState", "ReservationResult"]
