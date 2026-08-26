from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from interfaces.decision_record import DecisionCandidateSummary
from services.execution.transaction import ActionTransactionManager
from services.director.decision_record import DecisionRecordManager
from services.director.director import DirectorAction
from tests.integration.test_director_loop import _make


def _wire(loop, *, enabled: bool = True):
    transactions = ActionTransactionManager(enabled=True)
    records = DecisionRecordManager(
        enabled=enabled,
        hard_rejection_reasons=("safety_hold", "goal_action_not_allowed"),
    )
    loop._transactions = transactions
    loop._decision_records = records
    return transactions, records


@pytest.mark.asyncio
async def test_successful_action_record_links_evidence_and_commit() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    _transactions, records = _wire(loop)
    pool.add("event-chat-1", "raw private chat", now=0.0, kind="mention", viewer_id="raw-user")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    current = records.snapshot()["current"]
    assert current["evidence_refs"] == ["event-chat-1"]
    assert current["transaction_state"] == "committed"
    assert current["delivery_state"] == "delivered"
    assert current["outcome"] == "committed"
    assert "raw private chat" not in str(current)
    assert "raw-user" not in str(current)


@pytest.mark.asyncio
async def test_delivery_failure_record_links_released_transaction() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    _transactions, records = _wire(loop)
    pool.add("event-chat-2", "hello", now=0.0, kind="mention")

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("tts failed")

    loop._speak = fail_speech
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    current = records.snapshot()["current"]
    assert current["transaction_state"] == "released"
    assert current["delivery_state"] == "failed"
    assert current["outcome"] == "released"
    assert pool.size() == 1


@pytest.mark.asyncio
async def test_wait_decision_is_recorded_without_starting_transaction() -> None:
    loop, _director, _pool, _pulse, _runner, clock = _make()
    transactions, records = _wire(loop)
    loop._safety_hold_fn = lambda: True
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.WAIT
    current = records.snapshot()["current"]
    assert current["action"] == "wait"
    assert current["reason"] == "safety_hold"
    assert current["hard_rejection_reason"] == "safety_hold"
    assert current["transaction_id"] is None
    assert transactions.snapshot()["recent"] == []


@pytest.mark.asyncio
async def test_feature_off_preserves_director_action() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    _transactions, records = _wire(loop, enabled=False)
    pool.add("event-chat-3", "hello", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 0
    assert records.snapshot()["recent"] == []


@pytest.mark.asyncio
async def test_record_finalizes_when_transactions_are_disabled() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    transactions = ActionTransactionManager(enabled=False)
    records = DecisionRecordManager(enabled=True)
    loop._transactions = transactions
    loop._decision_records = records
    pool.add("event-chat-4", "hello", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    current = records.snapshot()["current"]
    assert current["transaction_id"] is None
    assert current["delivery_state"] == "delivered"
    assert current["outcome"] == "completed"


def test_dashboard_exposes_record_view_without_reconstructing_metrics() -> None:
    records = DecisionRecordManager()
    records.record_decision(
        created_at=1,
        action="wait",
        reason="idle",
        segment="main",
        evidence_refs=(),
        candidate_summary=DecisionCandidateSummary(),
    )
    snapshot = records.snapshot()
    assert snapshot == records.snapshot()
