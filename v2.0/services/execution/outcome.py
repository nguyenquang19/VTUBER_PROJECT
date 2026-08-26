"""Single terminal transaction and verified-outcome owner for S5."""
from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.execution import (
    ActionTransactionState,
    ExecutionReservation,
    OutcomeCommit,
    OutcomeCommitterService,
    OutcomeDisposition,
    VerifiedExecution,
)


class OutcomeCommitter(OutcomeCommitterService):
    """Terminalize reservations once and publish only after authoritative commit."""

    service_id = "outcome_committer"

    def __init__(
        self,
        transactions: Any,
        *,
        max_recent: int,
        max_reason_chars: int,
        max_evidence_refs: int,
        metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        publisher: Callable[[OutcomeCommit], None] | None = None,
    ) -> None:
        for name, value in (
            ("max_recent", max_recent),
            ("max_reason_chars", max_reason_chars),
            ("max_evidence_refs", max_evidence_refs),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._transactions = transactions
        self._max_reason_chars = max_reason_chars
        self._max_evidence_refs = max_evidence_refs
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._publisher = publisher
        self._recent: deque[OutcomeCommit] = deque(maxlen=max_recent)
        self._projection_failures: deque[dict[str, str]] = deque(maxlen=max_recent)
        self._counts: dict[str, int] = {}
        self._running = False

    @classmethod
    def from_loader(cls, loader: Any, transactions: Any, **kwargs: Any) -> "OutcomeCommitter":
        raw = loader.get("execution", "transactions", {})
        if not isinstance(raw, dict):
            raise ValueError("execution.transactions must be a mapping")
        return cls(
            transactions,
            max_recent=raw["max_recent_outcomes"],
            max_reason_chars=raw["max_reason_chars"],
            max_evidence_refs=raw["max_evidence_refs"],
            **kwargs,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self):
        from interfaces.base import HealthStatus
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, retained=len(self._recent))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "outcome_committer_recent": len(self._recent),
            "outcome_committer_total": dict(sorted(self._counts.items())),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "recent": [self._serialize(value) for value in self._recent],
            "projection_failures": list(self._projection_failures),
            "counts": dict(sorted(self._counts.items())),
        }

    def reserve(self, action_type: str, idempotency_key: str) -> ExecutionReservation:
        value = self._transactions.reserve(action_type, idempotency_key)
        transaction = value.transaction
        return ExecutionReservation(
            schema_version=1,
            execution_id=f"execution:{transaction.transaction_id}",
            transaction_id=transaction.transaction_id,
            action_type=transaction.action,
            idempotency_key=transaction.idempotency_key,
            created=value.created,
            reserved_at=datetime.fromtimestamp(transaction.created_at, timezone.utc),
        )

    def reservation_for(self, transaction_id: str) -> ExecutionReservation:
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id}")
        return ExecutionReservation(
            schema_version=1,
            execution_id=f"execution:{transaction.transaction_id}",
            transaction_id=transaction.transaction_id,
            action_type=transaction.action,
            idempotency_key=transaction.idempotency_key,
            created=transaction.state is not ActionTransactionState.COMMITTED,
            reserved_at=datetime.fromtimestamp(transaction.created_at, timezone.utc),
        )

    def mark_generated(self, reservation: ExecutionReservation) -> None:
        self._require_active(reservation)
        self._transactions.mark_generated(reservation.transaction_id)

    def mark_delivering(self, reservation: ExecutionReservation) -> None:
        self._require_active(reservation)
        self._transactions.mark_delivering(reservation.transaction_id)

    def commit_verified(
        self, reservation: ExecutionReservation, verified: VerifiedExecution,
    ) -> OutcomeCommit:
        self._match(reservation, verified)
        current = self._transactions.get(reservation.transaction_id)
        if current is None:
            raise KeyError("transaction disappeared before commit")
        if current.state is ActionTransactionState.COMMITTED:
            return self._finish(
                reservation, OutcomeDisposition.DUPLICATE_COMMITTED,
                "duplicate_committed", verified.verification.evidence_refs,
            )
        if current.state is not ActionTransactionState.DELIVERING:
            raise ValueError("verified commit requires DELIVERING transaction")
        self._transactions.mark_delivered(reservation.transaction_id)
        try:
            committed = self._transactions.commit(reservation.transaction_id)
        except Exception:
            committed = self._transactions.get(reservation.transaction_id)
            if committed is None or committed.state is not ActionTransactionState.COMMITTED:
                raise
        if committed.state is not ActionTransactionState.COMMITTED:
            raise RuntimeError("transaction did not reach COMMITTED")
        return self._finish(
            reservation, OutcomeDisposition.COMMITTED, "verified_committed",
            verified.verification.evidence_refs,
        )

    def release(
        self, reservation: ExecutionReservation, reason_code: str,
    ) -> OutcomeCommit:
        reason = self._reason(reason_code)
        current = self._transactions.get(reservation.transaction_id)
        if current is None:
            raise KeyError("transaction disappeared before release")
        if current.state is ActionTransactionState.COMMITTED:
            return self._finish(
                reservation, OutcomeDisposition.DUPLICATE_COMMITTED,
                "duplicate_committed", (),
            )
        if current.state is ActionTransactionState.RELEASED:
            return self._finish(reservation, OutcomeDisposition.RELEASED, reason, ())
        released = self._transactions.release(reservation.transaction_id, reason)
        if released.state is not ActionTransactionState.RELEASED:
            raise RuntimeError("transaction did not reach RELEASED")
        return self._finish(reservation, OutcomeDisposition.RELEASED, reason, ())

    def _require_active(self, reservation: ExecutionReservation) -> None:
        current = self._transactions.get(reservation.transaction_id)
        if current is None or current.state in {
            ActionTransactionState.COMMITTED, ActionTransactionState.RELEASED,
        }:
            raise ValueError("transaction is not active")

    @staticmethod
    def _match(reservation: ExecutionReservation, verified: VerifiedExecution) -> None:
        if (
            reservation.execution_id != verified.execution_id
            or reservation.transaction_id != verified.transaction_id
        ):
            raise ValueError("verified execution does not match reservation")

    def _finish(
        self,
        reservation: ExecutionReservation,
        disposition: OutcomeDisposition,
        reason_code: str,
        evidence_refs: tuple[str, ...],
    ) -> OutcomeCommit:
        evidence = tuple(dict.fromkeys(evidence_refs))[:self._max_evidence_refs]
        completed = self._clock().astimezone(timezone.utc)
        identity = "\n".join((
            reservation.execution_id, reservation.transaction_id,
            disposition.value, reason_code, completed.isoformat(),
        ))
        outcome = OutcomeCommit(
            schema_version=1,
            outcome_ref=f"outcome:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}",
            execution_id=reservation.execution_id,
            transaction_id=reservation.transaction_id,
            disposition=disposition,
            reason_code=self._reason(reason_code),
            evidence_refs=evidence,
            completed_at=completed,
        )
        self._recent.append(outcome)
        self._counts[disposition.value] = self._counts.get(disposition.value, 0) + 1
        recorder = getattr(self._metrics, "record_execution_outcome", None)
        if callable(recorder):
            try:
                recorder(disposition.value, outcome.reason_code)
            except Exception:
                pass
        if disposition is OutcomeDisposition.COMMITTED and self._publisher is not None:
            try:
                self._publisher(outcome)
            except Exception:
                self.record_projection_failure(reservation, "canonical_publisher")
        return outcome

    def record_projection_failure(
        self, reservation: ExecutionReservation, source: str,
    ) -> None:
        current = self._transactions.get(reservation.transaction_id)
        if current is None or current.state is not ActionTransactionState.COMMITTED:
            raise ValueError("projection failure can only follow committed outcome")
        normalized_source = self._reason(source)
        self._projection_failures.append({
            "transaction_id": reservation.transaction_id,
            "source": normalized_source,
            "recorded_at": self._clock().astimezone(timezone.utc).isoformat(),
        })
        self._counts["PROJECTION_FAILED"] = self._counts.get("PROJECTION_FAILED", 0) + 1
        recorder = getattr(self._metrics, "record_execution_projection_failure", None)
        if callable(recorder):
            try:
                recorder()
            except Exception:
                pass

    def _reason(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reason_code must be non-empty")
        return value.strip()[:self._max_reason_chars]

    @staticmethod
    def _serialize(value: OutcomeCommit) -> dict[str, Any]:
        return {
            "outcome_ref": value.outcome_ref,
            "execution_id": value.execution_id,
            "transaction_id": value.transaction_id,
            "disposition": value.disposition.value,
            "reason_code": value.reason_code,
            "evidence_refs": list(value.evidence_refs),
            "completed_at": value.completed_at.isoformat(),
        }
