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
        self, *, default_outcome: str, max_connected_guests: int,
        outcome_provider: Callable[[ActionRequest], str] | None = None,
    ) -> None:
        if default_outcome not in {"success", "failed"}:
            raise ValueError("default_outcome must be success or failed")
        if (
            isinstance(max_connected_guests, bool)
            or not isinstance(max_connected_guests, int)
            or max_connected_guests <= 0
        ):
            raise ValueError("max_connected_guests must be a positive int")
        if outcome_provider is not None and not callable(outcome_provider):
            raise ValueError("outcome_provider must be callable")
        self._outcome_provider = outcome_provider or (lambda _request: default_outcome)
        self._max_connected_guests = max_connected_guests
        self._connected: set[str] = set()

    def connected(self, guest_id: str) -> bool:
        return _required_target(guest_id) in self._connected

    async def apply(self, request: ActionRequest) -> tuple[bool, str]:
        outcome = self._outcome_provider(request)
        if not isinstance(outcome, str) or outcome not in {"success", "failed"}:
            return False, "invalid_outcome"
        if outcome != "success":
            return False, outcome
        try:
            guest_id = _canonical_guest_id(request)
        except ValueError:
            return False, "invalid_target"
        if request.action_type == "CALL_GUEST":
            if guest_id not in self._connected and len(self._connected) >= self._max_connected_guests:
                return False, "capacity_exceeded"
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
        try:
            guest_id = _canonical_guest_id(request)
        except ValueError:
            return VerificationResult(False, self.service_id, "invalid_target")
        if result.status is not ActionStatus.SUCCESS:
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


def _required_target(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("guest_id must be a non-empty string")
    return value.strip()


def _canonical_guest_id(request: ActionRequest) -> str:
    target = _required_target(request.target)
    guest_id = _required_target(request.arguments.get("guest_id"))
    if target != guest_id:
        raise ValueError("target and guest_id must match")
    return target
