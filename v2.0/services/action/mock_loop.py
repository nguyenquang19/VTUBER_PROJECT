"""Bounded generic action coordinator for the Phase 5 mock-only closed loop."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.action_execution import (
    ActionExecutor,
    ActionVerifier,
    GeneralActionService,
    VerificationResult,
)
from interfaces.action_transaction import ActionTransaction, ActionTransactionState
from interfaces.base import HealthStatus
from interfaces.compatibility import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    EventProvenance,
    PerceptionEvent,
)


_MOCK_ACTIONS = frozenset({"CALL_GUEST", "REMOVE_GUEST"})
_OUTCOMES = frozenset({"success", "failed"})


@dataclass(frozen=True)
class ActionMockConfig:
    execution_timeout_s: float
    max_recent_results: int
    max_idempotency_records: int
    max_connected_guests: int
    max_verification_evidence_refs: int
    default_outcome: str

    def __post_init__(self) -> None:
        timeout = self.execution_timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("execution_timeout_s must be a finite positive number")
        object.__setattr__(self, "execution_timeout_s", float(timeout))
        for name in (
            "max_recent_results",
            "max_idempotency_records",
            "max_connected_guests",
            "max_verification_evidence_refs",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        if self.max_idempotency_records < self.max_recent_results:
            raise ValueError("max_idempotency_records must cover max_recent_results")
        if not isinstance(self.default_outcome, str) or self.default_outcome not in _OUTCOMES:
            raise ValueError("default_outcome must be success or failed")

    @classmethod
    def from_loader(cls, loader: Any) -> "ActionMockConfig":
        raw = loader.get("capabilities", "mock_action", {})
        if not isinstance(raw, Mapping):
            raise ValueError("mock_action config must be a mapping")
        return cls(
            execution_timeout_s=raw.get("execution_timeout_s"),
            max_recent_results=raw.get("max_recent_results"),
            max_idempotency_records=raw.get("max_idempotency_records"),
            max_connected_guests=raw.get("max_connected_guests"),
            max_verification_evidence_refs=raw.get("max_verification_evidence_refs"),
            default_outcome=raw.get("default_outcome"),
        )


@dataclass(frozen=True)
class _IdempotencyRecord:
    fingerprint: str
    action_type: str
    result: ActionResult


class GeneralActionMockLoop(GeneralActionService):
    """Run a mock action transaction without owning any production action route."""

    service_id = "action_mock_closed_loop"

    def __init__(
        self,
        config: ActionMockConfig,
        *,
        capability_registry: Any,
        transactions: Any,
        world_model: Any,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, ActionMockConfig):
            raise ValueError("config must be ActionMockConfig")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._registry = capability_registry
        self._transactions = transactions
        self._world_model = world_model
        self._metrics = metrics
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._executors: dict[str, ActionExecutor] = {}
        self._verifiers: dict[str, ActionVerifier] = {}
        self._results: OrderedDict[int, ActionResult] = OrderedDict()
        self._idempotency: OrderedDict[str, _IdempotencyRecord] = OrderedDict()
        self._result_sequence = 0
        self._lock = asyncio.Lock()
        self._running = False
        self._outcomes: dict[str, int] = {}
        self._world_projection_inconsistencies = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "GeneralActionMockLoop":
        return cls(ActionMockConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled

    def register_executor(self, executor_id: str, executor: ActionExecutor) -> None:
        value = _required_string(executor_id, "executor_id")
        if not isinstance(executor, ActionExecutor):
            raise ValueError("executor must implement ActionExecutor")
        if value not in self._declared_adapter_ids("executor_id"):
            raise ValueError(f"undeclared mock executor_id: {value}")
        if value in self._executors:
            raise ValueError(f"duplicate executor_id: {value}")
        self._executors[value] = executor

    def register_verifier(self, verifier_id: str, verifier: ActionVerifier) -> None:
        value = _required_string(verifier_id, "verifier_id")
        if not isinstance(verifier, ActionVerifier):
            raise ValueError("verifier must implement ActionVerifier")
        if value not in self._declared_adapter_ids("verifier_id"):
            raise ValueError(f"undeclared mock verifier_id: {value}")
        if value in self._verifiers:
            raise ValueError(f"duplicate verifier_id: {value}")
        self._verifiers[value] = verifier

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for service in (*self._executors.values(), *self._verifiers.values()):
            await service.start()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for service in (*self._verifiers.values(), *self._executors.values()):
            await service.stop()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "mock action loop disabled")
        return HealthStatus.healthy(self.service_id, executors=len(self._executors))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "action_mock_enabled": self._enabled,
            "action_mock_outcomes": dict(sorted(self._outcomes.items())),
            "action_mock_recent_results": len(self._results),
            "action_mock_idempotency_records": len(self._idempotency),
            "action_mock_world_projection_inconsistencies": self._world_projection_inconsistencies,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "recent": [result.to_dict() for result in self._results.values()],
        }

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        fingerprint = _request_fingerprint(request)
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                self._idempotency.move_to_end(request.idempotency_key)
                if cached.fingerprint != fingerprint:
                    result = self._rejected(request, "idempotency_conflict")
                    return self._finish_untracked(result, "idempotency_conflict")
                self._record("duplicate")
                return cached.result
            transaction_source_ok, existing = self._find_transaction(request.idempotency_key)
            if not transaction_source_ok:
                result = self._rejected(request, "transaction_source_failed")
                return self._finish(request, fingerprint, result, "rejected")
            if existing is not None and existing.state is ActionTransactionState.COMMITTED:
                reason = (
                    "duplicate_result_unavailable"
                    if existing.action == request.action_type
                    else "idempotency_conflict"
                )
                result = self._rejected(request, reason)
                return self._finish(request, fingerprint, result, reason)
            return await self._execute_once(request, fingerprint)

    async def _execute_once(self, request: ActionRequest, fingerprint: str) -> ActionResult:
        rejected = self._validate(request)
        if rejected is not None:
            return self._finish(request, fingerprint, rejected, "rejected")
        capability = self._registry.capability(request.capability_id)
        if capability is None:
            return self._finish(
                request, fingerprint, self._rejected(request, "unknown_capability"), "rejected",
            )
        executor = self._executors[capability.executor_id]
        verifier = self._verifiers[capability.verifier_id]
        try:
            reservation = self._transactions.reserve(
                request.action_type, request.idempotency_key,
            )
        except Exception:
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, "reservation_failed"),
                "reservation_failed",
            )
        if not reservation.created:
            return self._finish(
                request, fingerprint,
                self._rejected(request, "duplicate_result_unavailable"),
                "duplicate_result_unavailable",
            )
        transaction_id = reservation.transaction.transaction_id
        try:
            self._transactions.mark_generated(transaction_id)
            self._transactions.mark_delivering(transaction_id)
        except Exception:
            self._safe_release(transaction_id, "transaction_transition_failed")
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, "transaction_transition_failed"),
                "released",
            )
        try:
            result = await asyncio.wait_for(
                executor.execute(request), timeout=self._config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            self._safe_release(transaction_id, "cancelled")
            raise
        except asyncio.TimeoutError:
            self._safe_release(transaction_id, "execution_timeout")
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.TIMEOUT, "execution_timeout"),
                "released",
            )
        except Exception as exc:
            self._safe_release(transaction_id, "executor_exception")
            return self._finish(
                request, fingerprint,
                self._failed(
                    request, ActionStatus.FAILED,
                    f"executor_{type(exc).__name__.lower()}",
                ),
                "released",
            )
        executor_error = _executor_result_error(request, result)
        if executor_error is not None:
            self._safe_release(transaction_id, "executor_failed")
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, executor_error),
                "released",
            )
        try:
            verification = await asyncio.wait_for(
                verifier.verify(request, result), timeout=self._config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            self._safe_release(transaction_id, "cancelled")
            raise
        except asyncio.TimeoutError:
            self._safe_release(transaction_id, "verification_timeout")
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, "verification_timeout"),
                "released",
            )
        except Exception:
            self._safe_release(transaction_id, "verification_unknown")
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, "verification_unknown"),
                "released",
            )
        verification_error = self._verification_error(verification)
        if verification_error is not None:
            self._safe_release(transaction_id, verification_error)
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, verification_error),
                "released",
            )
        if not verification.verified:
            self._safe_release(transaction_id, verification.reason_code)
            return self._finish(
                request, fingerprint,
                self._failed(request, ActionStatus.FAILED, verification.reason_code),
                "released",
            )
        try:
            self._transactions.mark_delivered(transaction_id)
            committed = self._transactions.commit(transaction_id)
        except Exception:
            current = self._get_transaction(transaction_id)
            if current is None or current.state is not ActionTransactionState.COMMITTED:
                self._safe_release(transaction_id, "final_commit_failed")
                return self._finish(
                    request, fingerprint,
                    self._failed(request, ActionStatus.FAILED, "final_commit_failed"),
                    "released",
                )
        else:
            current = self._get_transaction(transaction_id) or committed
            if current.state is not ActionTransactionState.COMMITTED:
                self._safe_release(transaction_id, "final_commit_failed")
                return self._finish(
                    request, fingerprint,
                    self._failed(request, ActionStatus.FAILED, "final_commit_failed"),
                    "released",
                )

        world_projected, projection_error = self._publish_world(
            request, verification.evidence_refs,
        )
        verified = ActionResult(
            schema_version=1,
            action_id=result.action_id,
            status=ActionStatus.SUCCESS,
            started_at=result.started_at,
            completed_at=result.completed_at,
            verified=True,
            verification_source=verification.source,
            result_data={
                **dict(result.result_data),
                "mock_only": True,
                "world_projected": world_projected,
            },
            error_code=projection_error,
        )
        outcome = "verified" if world_projected else "world_projection_inconsistent"
        terminal = self._finish(request, fingerprint, verified, outcome)
        if not world_projected:
            self._record_world_projection_inconsistency()
        return terminal

    def _validate(self, request: ActionRequest) -> ActionResult | None:
        if not self._enabled:
            return self._rejected(request, "feature_disabled")
        if request.schema_version != 1:
            return self._rejected(request, "unsupported_schema")
        if request.action_type not in _MOCK_ACTIONS:
            return self._rejected(request, "unsupported_action")
        if not _target_matches(request):
            return self._rejected(request, "invalid_target")
        if len(request.evidence_refs) > self._config.max_verification_evidence_refs:
            return self._rejected(request, "too_many_evidence_refs")
        try:
            capability = self._registry.capability(request.capability_id)
        except Exception:
            return self._rejected(request, "capability_source_failed")
        if capability is None:
            return self._rejected(request, "unknown_capability")
        if (
            request.action_type != capability.action_type
            or request.transaction_policy != capability.transaction_policy
        ):
            return self._rejected(request, "capability_mismatch")
        try:
            availability = self._registry.availability(request.capability_id)
        except Exception:
            return self._rejected(request, "capability_unavailable")
        if not availability.available:
            return self._rejected(request, availability.reason_code)
        if not _arguments_match(request.arguments, capability.parameter_schema):
            return self._rejected(request, "invalid_arguments")
        if capability.executor_id not in self._executors:
            return self._rejected(request, "missing_executor")
        if capability.verifier_id not in self._verifiers:
            return self._rejected(request, "missing_verifier")
        self._record("validated")
        return None

    def _verification_error(self, verification: object) -> str | None:
        if not isinstance(verification, VerificationResult):
            return "invalid_verification_result"
        if len(verification.evidence_refs) > self._config.max_verification_evidence_refs:
            return "too_many_verification_evidence_refs"
        return None

    def _publish_world(
        self, request: ActionRequest, evidence_refs: tuple[str, ...],
    ) -> tuple[bool, str | None]:
        value = request.action_type == "CALL_GUEST"
        try:
            event = PerceptionEvent(
                schema_version=1,
                event_id=f"action:{request.action_id}:world",
                source="runtime",
                event_type="world.observation",
                timestamp=_utc(self._clock()),
                payload={
                    "path": "call.guest_connected",
                    "value": value,
                    "evidence_refs": list(evidence_refs),
                },
                provenance=EventProvenance(
                    producer=self.service_id, source_event_id=request.action_id,
                ),
                confidence=1.0,
            )
            applied = self._world_model.apply_event(event)
        except Exception:
            return False, "world_projection_exception"
        if applied is True:
            return True, None
        if applied is False:
            return False, "world_projection_rejected"
        return False, "world_projection_malformed"

    def _declared_adapter_ids(self, field_name: str) -> frozenset[str]:
        try:
            snapshot = self._registry.snapshot()
            entries = snapshot["capabilities"]
        except Exception as exc:
            raise ValueError("capability declarations are unavailable") from exc
        if not isinstance(entries, list):
            raise ValueError("capability declarations are malformed")
        values: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("mock_only") is not True:
                continue
            capability = entry.get("capability")
            if not isinstance(capability, Mapping):
                raise ValueError("capability declaration is malformed")
            value = capability.get(field_name)
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
        return frozenset(values)

    def _find_transaction(
        self, idempotency_key: str,
    ) -> tuple[bool, ActionTransaction | None]:
        try:
            transaction = self._transactions.find_by_idempotency_key(idempotency_key)
        except Exception:
            return False, None
        if transaction is not None and not isinstance(transaction, ActionTransaction):
            return False, None
        return True, transaction

    def _get_transaction(self, transaction_id: str) -> ActionTransaction | None:
        try:
            transaction = self._transactions.get(transaction_id)
        except Exception:
            return None
        return transaction if isinstance(transaction, ActionTransaction) else None

    def _safe_release(self, transaction_id: str, reason: str) -> None:
        current = self._get_transaction(transaction_id)
        if current is None or current.state in {
            ActionTransactionState.COMMITTED,
            ActionTransactionState.RELEASED,
        }:
            return
        try:
            self._transactions.release(transaction_id, reason)
        except Exception:
            self._record("release_failed")

    def _rejected(self, request: ActionRequest, reason: str) -> ActionResult:
        return self._failed(request, ActionStatus.REJECTED, reason)

    def _failed(
        self, request: ActionRequest, status: ActionStatus, reason: str,
    ) -> ActionResult:
        now = _utc(self._clock())
        return ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=status,
            started_at=now,
            completed_at=now,
            verified=False,
            verification_source=None,
            result_data={"mock_only": True, "world_projected": False},
            error_code=reason,
        )

    def _finish(
        self,
        request: ActionRequest,
        fingerprint: str,
        result: ActionResult,
        outcome: str,
    ) -> ActionResult:
        self._idempotency[request.idempotency_key] = _IdempotencyRecord(
            fingerprint=fingerprint,
            action_type=request.action_type,
            result=result,
        )
        self._idempotency.move_to_end(request.idempotency_key)
        while len(self._idempotency) > self._config.max_idempotency_records:
            self._idempotency.popitem(last=False)
        return self._finish_untracked(result, outcome)

    def _finish_untracked(self, result: ActionResult, outcome: str) -> ActionResult:
        self._result_sequence += 1
        self._results[self._result_sequence] = result
        while len(self._results) > self._config.max_recent_results:
            self._results.popitem(last=False)
        self._record(outcome)
        return result

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        try:
            recorder = getattr(self._metrics, "record_action_mock_outcome", None)
            if callable(recorder):
                recorder(outcome)
        except Exception:
            pass

    def _record_world_projection_inconsistency(self) -> None:
        self._world_projection_inconsistencies += 1
        try:
            recorder = getattr(
                self._metrics,
                "record_action_mock_world_projection_inconsistency",
                None,
            )
            if callable(recorder):
                recorder()
        except Exception:
            pass


def _arguments_match(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    if set(arguments) != set(schema):
        return False
    return all(
        expected == "string"
        and isinstance(arguments.get(name), str)
        and bool(arguments[name].strip())
        for name, expected in schema.items()
    )


def _target_matches(request: ActionRequest) -> bool:
    target = request.target
    guest_id = request.arguments.get("guest_id")
    return (
        isinstance(target, str)
        and bool(target.strip())
        and target == target.strip()
        and isinstance(guest_id, str)
        and guest_id == target
    )


def _executor_result_error(request: ActionRequest, result: object) -> str | None:
    if not isinstance(result, ActionResult):
        return "invalid_executor_result"
    if result.action_id != request.action_id:
        return "executor_action_mismatch"
    if result.verified:
        return "executor_claimed_verification"
    if result.status is not ActionStatus.SUCCESS:
        return result.error_code or "executor_failed"
    return None


def _request_fingerprint(request: ActionRequest) -> str:
    payload = request.to_dict()
    payload.pop("requested_at", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("mock action clock must be timezone-aware")
    return value.astimezone(timezone.utc)
