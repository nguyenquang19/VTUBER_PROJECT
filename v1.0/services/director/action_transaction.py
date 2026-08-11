"""In-memory, bounded action transaction state machine for M10.1."""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from interfaces.action_transaction import (
    ActionTransaction,
    ActionTransactionService,
    ActionTransactionState,
    ReservationResult,
)
from interfaces.base import HealthStatus


_ALLOWED: dict[ActionTransactionState, set[ActionTransactionState]] = {
    ActionTransactionState.RESERVED: {
        ActionTransactionState.GENERATED, ActionTransactionState.RELEASED,
    },
    ActionTransactionState.GENERATED: {
        ActionTransactionState.DELIVERING, ActionTransactionState.RELEASED,
    },
    ActionTransactionState.DELIVERING: {
        ActionTransactionState.DELIVERED, ActionTransactionState.RELEASED,
    },
    ActionTransactionState.DELIVERED: {
        ActionTransactionState.COMMITTED, ActionTransactionState.RELEASED,
    },
    ActionTransactionState.COMMITTED: set(),
    ActionTransactionState.RELEASED: set(),
}


class ActionTransactionManager(ActionTransactionService):
    service_id = "action_transactions"

    def __init__(
        self,
        *,
        max_recent: int = 256,
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        enabled: bool = True,
    ) -> None:
        if max_recent <= 0:
            raise ValueError("action transaction max_recent must be positive")
        self.max_recent = int(max_recent)
        self._clock = clock or time.time
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._items: OrderedDict[str, ActionTransaction] = OrderedDict()
        self._by_key: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool = True,
    ) -> "ActionTransactionManager":
        return cls(
            max_recent=int(loader.get(
                "director", "director.transactions.max_recent", 256,
            )),
            metrics=metrics,
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, retained=len(self._items),
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def reserve(self, action: str, idempotency_key: str) -> ReservationResult:
        action_value = str(action).strip()
        key = str(idempotency_key).strip()
        if not action_value or not key:
            raise ValueError("action and idempotency_key are required")
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            existing = self._items.get(existing_id)
            if existing is not None and existing.state is ActionTransactionState.COMMITTED:
                self._record("duplicate_committed")
                return ReservationResult(transaction=existing, created=False)
        now = self._clock()
        item = ActionTransaction(
            transaction_id=f"act_{uuid.uuid4().hex}",
            idempotency_key=key,
            action=action_value,
            state=ActionTransactionState.RESERVED,
            created_at=now,
            updated_at=now,
        )
        self._items[item.transaction_id] = item
        self._by_key[key] = item.transaction_id
        self._trim()
        self._record(item.state.value)
        return ReservationResult(transaction=item, created=True)

    def mark_generated(self, transaction_id: str) -> ActionTransaction:
        return self._transition(transaction_id, ActionTransactionState.GENERATED)

    def mark_delivering(self, transaction_id: str) -> ActionTransaction:
        return self._transition(transaction_id, ActionTransactionState.DELIVERING)

    def mark_delivered(self, transaction_id: str) -> ActionTransaction:
        return self._transition(transaction_id, ActionTransactionState.DELIVERED)

    def commit(self, transaction_id: str) -> ActionTransaction:
        item = self._require(transaction_id)
        if item.state is ActionTransactionState.COMMITTED:
            return item
        return self._transition(transaction_id, ActionTransactionState.COMMITTED)

    def release(self, transaction_id: str, reason: str) -> ActionTransaction:
        item = self._require(transaction_id)
        if item.state is ActionTransactionState.RELEASED:
            return item
        if item.state is ActionTransactionState.COMMITTED:
            raise ValueError("committed action transaction cannot be released")
        return self._transition(
            transaction_id, ActionTransactionState.RELEASED, reason=reason,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"action_transactions_{name}_total": count
            for name, count in sorted(self._counts.items())
        }

    def snapshot(self) -> dict[str, Any]:
        recent = list(self._items.values())[-20:]
        return {
            "enabled": self.enabled,
            "counts": dict(sorted(self._counts.items())),
            "recent": [item.model_dump(mode="json") for item in recent],
        }

    def _transition(
        self,
        transaction_id: str,
        target: ActionTransactionState,
        *,
        reason: str = "",
    ) -> ActionTransaction:
        item = self._require(transaction_id)
        if item.state is target:
            return item
        if target not in _ALLOWED[item.state]:
            raise ValueError(f"invalid action transaction transition {item.state} -> {target}")
        updated = item.model_copy(update={
            "state": target,
            "updated_at": self._clock(),
            "reason": str(reason)[:240],
        })
        self._items[transaction_id] = updated
        self._items.move_to_end(transaction_id)
        self._record(target.value)
        return updated

    def _require(self, transaction_id: str) -> ActionTransaction:
        try:
            return self._items[transaction_id]
        except KeyError as exc:
            raise KeyError(f"unknown action transaction {transaction_id}") from exc

    def _record(self, state: str) -> None:
        self._counts[state] = self._counts.get(state, 0) + 1
        if self._metrics is not None and hasattr(
            self._metrics, "record_action_transaction",
        ):
            self._metrics.record_action_transaction(state)

    def _trim(self) -> None:
        while len(self._items) > self.max_recent:
            transaction_id, item = self._items.popitem(last=False)
            if self._by_key.get(item.idempotency_key) == transaction_id:
                self._by_key.pop(item.idempotency_key, None)
