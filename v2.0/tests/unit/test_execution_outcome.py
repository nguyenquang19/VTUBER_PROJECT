from __future__ import annotations

from datetime import datetime, timezone

import pytest

from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.execution import (
    ActionTransactionState,
    OutcomeDisposition,
    VerificationResult,
    VerifiedExecution,
)
from services.execution.outcome import OutcomeCommitter
from services.execution.transaction import ActionTransactionManager


NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def _request() -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id="speech:1",
        capability_id="SPEAK",
        action_type="SPEAK",
        target=None,
        arguments={"text": "Chào nhé."},
        intention_id=None,
        evidence_refs=("delivery:speech:1",),
        idempotency_key="speech:1",
        priority=0.0,
        requested_at=NOW,
        transaction_policy="delivery_aware",
    )


def _verified(reservation) -> VerifiedExecution:
    request = _request()
    result = ActionResult(
        schema_version=1,
        action_id=request.action_id,
        status=ActionStatus.SUCCESS,
        started_at=NOW,
        completed_at=NOW,
        verified=True,
        verification_source="tts_delivery",
        result_data={},
        error_code=None,
    )
    verification = VerificationResult(
        verified=True,
        source="tts_delivery",
        reason_code="delivered",
        evidence_refs=request.evidence_refs,
    )
    return VerifiedExecution(
        schema_version=1,
        execution_id=reservation.execution_id,
        transaction_id=reservation.transaction_id,
        request=request,
        result=result,
        verification=verification,
        verified_at=NOW,
    )


def _committer(*, publisher=None) -> tuple[OutcomeCommitter, ActionTransactionManager]:
    transactions = ActionTransactionManager(clock=lambda: NOW.timestamp())
    committer = OutcomeCommitter(
        transactions,
        max_recent=8,
        max_reason_chars=32,
        max_evidence_refs=2,
        clock=lambda: NOW,
        publisher=publisher,
    )
    return committer, transactions


def test_verified_execution_is_required_before_terminal_commit() -> None:
    committer, transactions = _committer()
    reservation = committer.reserve("read_chat", "turn:1")

    with pytest.raises(ValueError, match="DELIVERING"):
        committer.commit_verified(reservation, _verified(reservation))

    committer.mark_generated(reservation)
    committer.mark_delivering(reservation)
    outcome = committer.commit_verified(reservation, _verified(reservation))

    assert outcome.disposition is OutcomeDisposition.COMMITTED
    assert transactions.get(reservation.transaction_id).state is ActionTransactionState.COMMITTED
    assert outcome.evidence_refs == ("delivery:speech:1",)


def test_unverified_path_releases_without_business_publication() -> None:
    published = []
    committer, transactions = _committer(publisher=published.append)
    reservation = committer.reserve("read_chat", "turn:2")
    committer.mark_generated(reservation)

    outcome = committer.release(reservation, "verification_failed")

    assert outcome.disposition is OutcomeDisposition.RELEASED
    assert transactions.get(reservation.transaction_id).state is ActionTransactionState.RELEASED
    assert published == []


def test_projection_failure_cannot_reverse_verified_commit() -> None:
    def fail_projection(_outcome) -> None:
        raise RuntimeError("projection failed")

    committer, transactions = _committer(publisher=fail_projection)
    reservation = committer.reserve("read_chat", "turn:3")
    committer.mark_generated(reservation)
    committer.mark_delivering(reservation)

    outcome = committer.commit_verified(reservation, _verified(reservation))

    assert outcome.disposition is OutcomeDisposition.COMMITTED
    assert transactions.get(reservation.transaction_id).state is ActionTransactionState.COMMITTED
    assert committer.get_metrics()["outcome_committer_total"]["PROJECTION_FAILED"] == 1


def test_committed_idempotency_key_never_creates_second_execution() -> None:
    committer, _transactions = _committer()
    first = committer.reserve("read_chat", "turn:4")
    committer.mark_generated(first)
    committer.mark_delivering(first)
    committer.commit_verified(first, _verified(first))

    duplicate = committer.reserve("read_chat", "turn:4")

    assert duplicate.created is False
    assert duplicate.transaction_id == first.transaction_id


def test_observer_exception_after_commit_cannot_hide_terminal_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committer, transactions = _committer()
    reservation = committer.reserve("read_chat", "turn:5")
    committer.mark_generated(reservation)
    committer.mark_delivering(reservation)
    original = transactions.commit

    def commit_then_raise(transaction_id: str):
        original(transaction_id)
        raise RuntimeError("observer failed")

    monkeypatch.setattr(transactions, "commit", commit_then_raise)

    outcome = committer.commit_verified(reservation, _verified(reservation))

    assert outcome.disposition is OutcomeDisposition.COMMITTED
    assert transactions.get(reservation.transaction_id).state is ActionTransactionState.COMMITTED
