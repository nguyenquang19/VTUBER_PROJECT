"""Deterministic in-memory authority for the Phase 5 mock guest executor."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from interfaces.action_execution import ActionExecutor, ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus


class MockCallBackend:
    """Private simulated external call state; verifier reads it independently."""

    def __init__(
        self, *, default_outcome: str,
        outcome_provider: Callable[[ActionRequest], str] | None = None,
    ) -> None:
        self._outcome_provider = outcome_provider or (lambda _request: default_outcome)
        self._connected: set[str] = set()

    def connected(self, guest_id: str) -> bool:
        return str(guest_id).strip() in self._connected

    async def apply(self, request: ActionRequest) -> tuple[bool, str]:
        outcome = str(self._outcome_provider(request)).strip().lower()
        if outcome != "success":
            return False, outcome or "failed"
        guest_id = str(request.target or request.arguments.get("guest_id") or "").strip()
        if not guest_id:
            return False, "invalid_target"
        if request.action_type == "CALL_GUEST":
            self._connected.add(guest_id)
            return True, "success"
        if request.action_type == "REMOVE_GUEST":
            self._connected.discard(guest_id)
            return True, "success"
        return False, "unsupported_action"


class MockCallExecutor(ActionExecutor):
    service_id = "mock_call"

    def __init__(
        self, backend: MockCallBackend, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id) if self._running else HealthStatus.stopped(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {}

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = _utc(self._clock())
        ok, reason = await self._backend.apply(request)
        completed = _utc(self._clock())
        return ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=ActionStatus.SUCCESS if ok else ActionStatus.FAILED,
            started_at=started,
            completed_at=completed,
            verified=False,
            verification_source=None,
            result_data={"target": request.target, "mock_only": True},
            error_code=None if ok else f"mock_{reason}",
        )


class MockCallVerifier(ActionVerifier):
    service_id = "mock_call"

    def __init__(self, backend: MockCallBackend) -> None:
        self._backend = backend
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id) if self._running else HealthStatus.stopped(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {}

    async def verify(self, request: ActionRequest, result: ActionResult) -> VerificationResult:
        guest_id = str(request.target or request.arguments.get("guest_id") or "").strip()
        if result.status is not ActionStatus.SUCCESS or not guest_id:
            return VerificationResult(False, self.service_id, "executor_failed")
        expected = request.action_type == "CALL_GUEST"
        if request.action_type not in {"CALL_GUEST", "REMOVE_GUEST"}:
            return VerificationResult(False, self.service_id, "unsupported_action")
        if self._backend.connected(guest_id) != expected:
            return VerificationResult(False, self.service_id, "verification_unknown")
        return VerificationResult(True, self.service_id, "verified", (f"mock_guest:{guest_id}",))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("mock action clock must be timezone-aware")
    return value.astimezone(timezone.utc)
