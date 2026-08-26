from __future__ import annotations

from types import SimpleNamespace

import pytest

from interfaces.execution import ActionTransactionState
from services.execution.coordinator import ExecutionCoordinator
from services.execution.outcome import OutcomeCommitter
from services.execution.speech import DirectorDeliveryBoundary
from services.execution.transaction import ActionTransactionManager


class _Runner:
    last_filter_verdict = None

    def __init__(self) -> None:
        self.finalized: list[tuple[str, bool]] = []

    def finalize_delivery(self, request_id: str, success: bool) -> None:
        self.finalized.append((request_id, success))


class _Log:
    def warning(self, *_args, **_kwargs) -> None: pass


@pytest.mark.asyncio
async def test_compatibility_projection_observes_committed_transaction() -> None:
    transactions = ActionTransactionManager()
    committer = OutcomeCommitter(
        transactions,
        max_recent=8,
        max_reason_chars=32,
        max_evidence_refs=4,
    )
    reservation = committer.reserve("read_chat", "turn:ordered")
    projected_states: list[ActionTransactionState] = []

    async def speak(_request_id: str, _text: str):
        return SimpleNamespace(delivered=True, mode="audio")

    def project(*_args, **_kwargs) -> None:
        projected_states.append(
            transactions.get(reservation.transaction_id).state,
        )

    boundary = DirectorDeliveryBoundary(
        runner=_Runner(),
        speak=speak,
        transactions=transactions,
        execution_coordinator=ExecutionCoordinator(
            local_boundary=object(), outcome_committer=committer,
        ),
        outcome_committer=committer,
        mood_provider=lambda: SimpleNamespace(),
        speech_completed=project,
        filter_rejected=lambda **_kwargs: None,
        logger=_Log(),
    )

    delivered = await boundary.deliver(
        "speech:ordered",
        SimpleNamespace(text="Chào nhé."),
        "read_chat",
        [],
        transaction_id=reservation.transaction_id,
    )

    assert delivered is True
    assert projected_states == [ActionTransactionState.COMMITTED]


@pytest.mark.asyncio
async def test_post_commit_projection_error_never_releases_verified_delivery() -> None:
    transactions = ActionTransactionManager()
    committer = OutcomeCommitter(
        transactions,
        max_recent=8,
        max_reason_chars=32,
        max_evidence_refs=4,
    )
    reservation = committer.reserve("read_chat", "turn:projection-error")

    async def speak(_request_id: str, _text: str):
        return SimpleNamespace(delivered=True, mode="audio")

    def fail_projection(*_args, **_kwargs) -> None:
        raise RuntimeError("compatibility projection failed")

    boundary = DirectorDeliveryBoundary(
        runner=_Runner(),
        speak=speak,
        transactions=transactions,
        execution_coordinator=ExecutionCoordinator(
            local_boundary=object(), outcome_committer=committer,
        ),
        outcome_committer=committer,
        mood_provider=lambda: SimpleNamespace(),
        speech_completed=fail_projection,
        filter_rejected=lambda **_kwargs: None,
        logger=_Log(),
    )

    delivered = await boundary.deliver(
        "speech:projection-error",
        SimpleNamespace(text="Chào nhé."),
        "read_chat",
        [],
        transaction_id=reservation.transaction_id,
    )

    assert delivered is True
    assert transactions.get(reservation.transaction_id).state is ActionTransactionState.COMMITTED
    assert committer.get_metrics()["outcome_committer_total"]["PROJECTION_FAILED"] == 1
