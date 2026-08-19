from __future__ import annotations

import pytest

from interfaces.action_transaction import ActionTransactionState
from orchestrator.metrics_collector import MetricsCollector
from services.director.action_transaction import ActionTransactionManager


def test_transaction_follows_delivery_before_commit() -> None:
    clock = iter((1.0, 2.0, 3.0, 4.0, 5.0))
    manager = ActionTransactionManager(clock=lambda: next(clock))
    item = manager.reserve("read_chat", "read_chat:m1").transaction
    assert item.state is ActionTransactionState.RESERVED
    manager.mark_generated(item.transaction_id)
    manager.mark_delivering(item.transaction_id)
    manager.mark_delivered(item.transaction_id)
    committed = manager.commit(item.transaction_id)
    assert committed.state is ActionTransactionState.COMMITTED


def test_failed_transaction_releases_and_can_retry_same_key() -> None:
    manager = ActionTransactionManager()
    first = manager.reserve("read_chat", "read_chat:m1").transaction
    manager.release(first.transaction_id, "tts failed")
    second = manager.reserve("read_chat", "read_chat:m1")
    assert second.created is True
    assert second.transaction.transaction_id != first.transaction_id


def test_committed_idempotency_key_is_not_reserved_twice() -> None:
    manager = ActionTransactionManager()
    first = manager.reserve("transition", "transition:main").transaction
    manager.mark_generated(first.transaction_id)
    manager.mark_delivering(first.transaction_id)
    manager.mark_delivered(first.transaction_id)
    manager.commit(first.transaction_id)
    duplicate = manager.reserve("transition", "transition:main")
    assert duplicate.created is False
    assert duplicate.transaction.transaction_id == first.transaction_id
    with pytest.raises(ValueError, match="different action"):
        manager.reserve("other", "transition:main")


def test_invalid_transition_is_rejected() -> None:
    manager = ActionTransactionManager()
    item = manager.reserve("read_chat", "read_chat:m1").transaction
    with pytest.raises(ValueError, match="invalid action transaction transition"):
        manager.commit(item.transaction_id)


def test_transaction_metrics_and_snapshot_are_bounded() -> None:
    metrics = MetricsCollector()
    manager = ActionTransactionManager(max_recent=2, metrics=metrics)
    for index in range(3):
        manager.reserve("self_talk", f"self:{index}")
    assert len(manager.snapshot()["recent"]) == 2
    assert metrics.action_transaction_snapshot()["reserved"] == 3


def test_transaction_lookup_and_inputs_are_strict() -> None:
    manager = ActionTransactionManager()
    item = manager.reserve("read_chat", "read_chat:m1").transaction

    assert manager.get(item.transaction_id) == item
    assert manager.find_by_idempotency_key("read_chat:m1") == item
    assert manager.get("missing") is None
    with pytest.raises(ValueError, match="max_recent"):
        ActionTransactionManager(max_recent=True)
    with pytest.raises(ValueError, match="max_recent"):
        ActionTransactionManager(max_recent="2")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="action must be"):
        manager.reserve(None, "key")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="idempotency_key must be"):
        manager.reserve("read_chat", 1)  # type: ignore[arg-type]


def test_transaction_metric_failure_does_not_interrupt_state_change() -> None:
    class BrokenMetrics:
        def record_action_transaction(self, _state: str) -> None:
            raise RuntimeError("metrics unavailable")

    manager = ActionTransactionManager(metrics=BrokenMetrics())
    item = manager.reserve("read_chat", "read_chat:m1").transaction
    manager.mark_generated(item.transaction_id)
    manager.mark_delivering(item.transaction_id)
    manager.mark_delivered(item.transaction_id)
    committed = manager.commit(item.transaction_id)

    assert committed.state is ActionTransactionState.COMMITTED
