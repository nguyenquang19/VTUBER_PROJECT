"""Canonical typed local execution coordinator for S5."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from interfaces.compatibility import ActionRequest, ActionStatus
from interfaces.execution import (
    ExecutionBoundaryService,
    ExecutionReservation,
    VerificationResult,
    VerifiedExecution,
)


class ExecutionCoordinator(ExecutionBoundaryService):
    """Reserve and execute/verify locally; terminal commit stays in OutcomeCommitter."""

    service_id = "execution_coordinator"

    def __init__(self, *, local_boundary: Any, outcome_committer: Any) -> None:
        self._local = local_boundary
        self._outcome = outcome_committer
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self):
        from interfaces.base import HealthStatus
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {}

    def reserve(self, action_type: str, idempotency_key: str) -> ExecutionReservation:
        return self._outcome.reserve(action_type, idempotency_key)

    async def execute_verified(
        self, reservation: ExecutionReservation, request: ActionRequest,
    ) -> VerifiedExecution | None:
        self._outcome.mark_generated(reservation)
        self._outcome.mark_delivering(reservation)
        result = await self._local.execute(request)
        if (
            result.action_id != request.action_id
            or result.status is not ActionStatus.SUCCESS
            or not result.verified
            or result.verification_source is None
        ):
            return None
        verification = VerificationResult(
            verified=True,
            source=result.verification_source,
            reason_code="verified",
            evidence_refs=request.evidence_refs,
        )
        return VerifiedExecution(
            schema_version=1,
            execution_id=reservation.execution_id,
            transaction_id=reservation.transaction_id,
            request=request,
            result=result,
            verification=verification,
            verified_at=datetime.now(timezone.utc),
        )
