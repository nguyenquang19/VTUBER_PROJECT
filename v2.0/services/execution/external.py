"""Verified transaction coordinator for the Phase 9 OBS external action."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.execution import (
    ActionVerifier,
    ActionTransaction,
    ActionTransactionState,
    ExternalActionExecutor,
    ExternalExecutorRegistryService,
    GeneralActionService,
    ReservationResult,
    RollbackResult,
    RollbackStatus,
    VerificationResult,
    VerifiedExecution,
)
from interfaces.base import HealthStatus
from interfaces.compatibility import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    Capability,
    CapabilityAvailability,
    EventProvenance,
    PerceptionEvent,
)


@dataclass(frozen=True)
class ExternalActionConfig:
    execution_timeout_s: float
    verification_timeout_s: float
    rollback_timeout_s: float
    max_recent_results: int
    max_idempotency_records: int
    max_verification_evidence_refs: int
    max_scene_name_chars: int
    max_registry_bindings: int

    def __post_init__(self) -> None:
        for name in ("execution_timeout_s", "verification_timeout_s", "rollback_timeout_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
            object.__setattr__(self, name, float(value))
        for name in (
            "max_recent_results", "max_idempotency_records",
            "max_verification_evidence_refs", "max_scene_name_chars",
            "max_registry_bindings",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        if self.max_idempotency_records < self.max_recent_results:
            raise ValueError("max_idempotency_records must cover max_recent_results")

    @classmethod
    def from_loader(cls, loader: Any) -> "ExternalActionConfig":
        raw = loader.get("execution", "external", None)
        obs = loader.get("execution", "external.obs", None)
        if not isinstance(raw, Mapping) or not isinstance(obs, Mapping):
            raise ValueError("execution.external config must be a mapping")
        return cls(
            execution_timeout_s=raw.get("execution_timeout_s"),
            verification_timeout_s=raw.get("verification_timeout_s"),
            rollback_timeout_s=raw.get("rollback_timeout_s"),
            max_recent_results=raw.get("max_recent_results"),
            max_idempotency_records=raw.get("max_idempotency_records"),
            max_verification_evidence_refs=raw.get("max_verification_evidence_refs"),
            max_scene_name_chars=obs.get("max_scene_name_chars"),
            max_registry_bindings=raw.get("max_registry_bindings"),
        )


@dataclass(frozen=True)
class _IdempotencyRecord:
    fingerprint: str
    action_type: str
    result: ActionResult


class ExternalActionLoop(GeneralActionService):
    """Coordinate only declared external actions; never selects an action itself."""

    service_id = "external_action_loop"

    def __init__(
        self,
        config: ExternalActionConfig,
        *,
        capability_registry: Any,
        executor_registry: ExternalExecutorRegistryService,
        transactions: Any,
        outcome_committer: Any = None,
        world_model: Any,
        metrics: Any = None,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, ExternalActionConfig):
            raise ValueError("config must be ExternalActionConfig")
        if not isinstance(executor_registry, ExternalExecutorRegistryService):
            raise ValueError("executor_registry must implement ExternalExecutorRegistryService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._capabilities = capability_registry
        self._executors = executor_registry
        self._transactions = transactions
        self._outcome_committer = outcome_committer
        self._world_model = world_model
        self._metrics = metrics
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._lock = asyncio.Lock()
        self._results: OrderedDict[int, ActionResult] = OrderedDict()
        self._idempotency: OrderedDict[str, _IdempotencyRecord] = OrderedDict()
        self._sequence = 0
        self._outcomes: dict[str, int] = {}
        self._rollback_outcomes: dict[str, int] = {}
        self._world_projection_inconsistencies = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        registry = await self._executors.health_check()
        if not registry.is_ok:
            return HealthStatus.degraded(self.service_id, "executor_registry_degraded")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "external_action_enabled": self._enabled,
            "external_action_running": self._running,
            "external_action_outcomes": dict(sorted(self._outcomes.items())),
            "external_action_rollback_outcomes": dict(sorted(self._rollback_outcomes.items())),
            "external_action_recent_results": len(self._results),
            "external_action_idempotency_records": len(self._idempotency),
            "external_action_world_projection_inconsistencies": (
                self._world_projection_inconsistencies
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "recent": [result.to_dict() for result in self._results.values()],
            "rollback_outcomes": dict(sorted(self._rollback_outcomes.items())),
        }

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        fingerprint = _fingerprint(request)
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
            if cached is not None:
                self._idempotency.move_to_end(request.idempotency_key)
                if cached.fingerprint != fingerprint:
                    return self._finish_untracked(
                        self._result(request, ActionStatus.REJECTED, "idempotency_conflict"),
                        "idempotency_conflict",
                    )
                self._record("idempotent_hit")
                return cached.result
            source_ok, existing = self._find_transaction(request.idempotency_key)
            if not source_ok:
                return self._finish(
                    request, fingerprint,
                    self._result(request, ActionStatus.REJECTED, "transaction_source_failed"),
                    "transaction_source_failed",
                )
            if existing is not None:
                reason = (
                    "idempotency_conflict"
                    if existing.action != request.action_type
                    else "duplicate_result_unavailable"
                )
                return self._finish(
                    request, fingerprint,
                    self._result(request, ActionStatus.REJECTED, reason), reason,
                )
            return await self._execute_once(request, fingerprint)

    async def _execute_once(self, request: ActionRequest, fingerprint: str) -> ActionResult:
        error, capability, executor, verifier = self._validate(request)
        if error is not None:
            return self._finish(
                request, fingerprint,
                self._result(request, ActionStatus.REJECTED, error), "rejected",
            )
        assert capability is not None and executor is not None and verifier is not None
        try:
            reservation = self._transactions.reserve(
                request.action_type, request.idempotency_key,
            )
        except Exception:
            return self._finish(
                request, fingerprint,
                self._result(request, ActionStatus.FAILED, "reservation_failed"),
                "reservation_failed",
            )
        if not isinstance(reservation, ReservationResult):
            return self._finish(
                request, fingerprint,
                self._result(request, ActionStatus.FAILED, "reservation_result_invalid"),
                "reservation_failed",
            )
        if not reservation.created:
            return self._finish(
                request, fingerprint,
                self._result(request, ActionStatus.REJECTED, "duplicate_result_unavailable"),
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
                self._result(request, ActionStatus.FAILED, "transaction_transition_failed"),
                "released",
            )

        execution_result: object
        try:
            execution_result = await asyncio.wait_for(
                executor.execute(request), timeout=self._config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            await self._rollback_cancelled(executor, request)
            self._safe_release(transaction_id, "cancelled")
            raise
        except asyncio.TimeoutError:
            execution_result = self._result(
                request, ActionStatus.TIMEOUT, "execution_timeout",
            )
        except Exception:
            execution_result = self._result(
                request, ActionStatus.UNKNOWN, "executor_exception",
            )
        executor_error = _executor_error(request, execution_result)
        if executor_error is not None:
            safe_result = (
                execution_result
                if isinstance(execution_result, ActionResult)
                and execution_result.action_id == request.action_id
                else self._result(request, ActionStatus.UNKNOWN, executor_error)
            )
            rollback = await self._safe_rollback(executor, request, safe_result)
            self._safe_release(transaction_id, executor_error)
            return self._finish(
                request, fingerprint,
                self._with_rollback(safe_result, executor_error, rollback),
                "released",
            )
        assert isinstance(execution_result, ActionResult)

        try:
            verification = await asyncio.wait_for(
                verifier.verify(request, execution_result),
                timeout=self._config.verification_timeout_s,
            )
        except asyncio.CancelledError:
            await self._rollback_cancelled(executor, request, execution_result)
            self._safe_release(transaction_id, "cancelled")
            raise
        except asyncio.TimeoutError:
            verification = None
            verification_error = "verification_timeout"
        except Exception:
            verification = None
            verification_error = "verification_unknown"
        else:
            verification_error = self._verification_error(verification)
        if verification_error is not None:
            rollback = await self._safe_rollback(executor, request, execution_result)
            self._safe_release(transaction_id, verification_error)
            return self._finish(
                request, fingerprint,
                self._with_rollback(execution_result, verification_error, rollback),
                "released",
            )
        assert isinstance(verification, VerificationResult)
        if not verification.verified:
            rollback = await self._safe_rollback(executor, request, execution_result)
            self._safe_release(transaction_id, verification.reason_code)
            return self._finish(
                request, fingerprint,
                self._with_rollback(
                    execution_result, verification.reason_code, rollback,
                ),
                "released",
            )

        try:
            if self._outcome_committer is not None:
                self._commit_verified(
                    transaction_id, request, execution_result, verification,
                )
            else:
                self._transactions.mark_delivered(transaction_id)
                self._transactions.commit(transaction_id)
        except Exception:
            current = self._transaction(transaction_id)
            if current is None or current.state is not ActionTransactionState.COMMITTED:
                rollback = await self._safe_rollback(executor, request, execution_result)
                self._safe_release(transaction_id, "final_commit_failed")
                return self._finish(
                    request, fingerprint,
                    self._with_rollback(
                        execution_result, "final_commit_failed", rollback,
                    ),
                    "released",
                )
        else:
            current = self._transaction(transaction_id)
            if current is None or current.state is not ActionTransactionState.COMMITTED:
                rollback = await self._safe_rollback(executor, request, execution_result)
                self._safe_release(transaction_id, "final_commit_failed")
                return self._finish(
                    request, fingerprint,
                    self._with_rollback(
                        execution_result, "final_commit_failed", rollback,
                    ),
                    "released",
                )

        world_projected, projection_error = self._publish_world(
            request, verification.evidence_refs,
        )
        data = {
            **dict(execution_result.result_data),
            "authoritative_scene": request.target,
            "world_projected": world_projected,
            "rollback_status": "not_required",
        }
        verified = ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=ActionStatus.SUCCESS,
            started_at=execution_result.started_at,
            completed_at=execution_result.completed_at,
            verified=True,
            verification_source=verification.source,
            result_data=data,
            error_code=projection_error,
        )
        if not world_projected:
            self._world_projection_inconsistencies += 1
            self._record("world_projection_inconsistent")
        return self._finish(
            request, fingerprint, verified,
            "verified" if world_projected else "world_projection_inconsistent",
        )

    def _validate(
        self, request: ActionRequest,
    ) -> tuple[
        str | None,
        Capability | None,
        ExternalActionExecutor | None,
        ActionVerifier | None,
    ]:
        if not self._running:
            return "coordinator_stopped", None, None, None
        if not self._enabled:
            return "feature_disabled", None, None, None
        if request.schema_version != 1:
            return "unsupported_schema", None, None, None
        if request.action_type != "SWITCH_SCENE" or request.capability_id != "SWITCH_SCENE":
            return "unsupported_action", None, None, None
        if request.transaction_policy != "verified":
            return "transaction_policy_mismatch", None, None, None
        if set(request.arguments) != {"scene_name"}:
            return "invalid_arguments", None, None, None
        scene = request.arguments.get("scene_name")
        if (
            not isinstance(request.target, str)
            or not isinstance(scene, str)
            or request.target != scene
            or request.target != request.target.strip()
            or not request.target
            or len(request.target) > self._config.max_scene_name_chars
            or any(ord(char) < 32 or ord(char) == 127 for char in request.target)
        ):
            return "invalid_target", None, None, None
        if len(request.evidence_refs) > self._config.max_verification_evidence_refs:
            return "too_many_evidence_refs", None, None, None
        try:
            capability = self._capabilities.capability(request.capability_id)
        except Exception:
            return "capability_source_failed", None, None, None
        if capability is None:
            return "unknown_capability", None, None, None
        if not isinstance(capability, Capability):
            return "capability_source_malformed", None, None, None
        if (
            capability.action_type != request.action_type
            or capability.transaction_policy != request.transaction_policy
            or dict(capability.parameter_schema) != {"scene_name": "string"}
        ):
            return "capability_mismatch", None, None, None
        try:
            availability = self._capabilities.availability(request.capability_id)
        except Exception:
            return "capability_unavailable", None, None, None
        if not isinstance(availability, CapabilityAvailability):
            return "capability_availability_malformed", None, None, None
        if not availability.available:
            return availability.reason_code, None, None, None
        try:
            executor = self._executors.executor_for(capability.executor_id)
            verifier = self._executors.verifier_for(capability.verifier_id)
            binding = self._executors.binding_for(capability.executor_id)
        except Exception:
            return "external_registry_failed", None, None, None
        if not isinstance(executor, ExternalActionExecutor):
            return "missing_executor", None, None, None
        if verifier is None:
            return "missing_verifier", None, None, None
        if (
            binding is None
            or binding.verifier_id != capability.verifier_id
            or binding.feature_id != "obs_scene_executor"
            or binding.health_target_id != "obs_websocket"
        ):
            return "route_mismatch", None, None, None
        self._record("validated")
        return None, capability, executor, verifier

    def _verification_error(self, value: object) -> str | None:
        if not isinstance(value, VerificationResult):
            return "invalid_verification_result"
        if len(value.evidence_refs) > self._config.max_verification_evidence_refs:
            return "too_many_verification_evidence_refs"
        return None

    async def _safe_rollback(
        self,
        executor: ExternalActionExecutor,
        request: ActionRequest,
        result: ActionResult,
    ) -> RollbackResult:
        try:
            rollback = await asyncio.wait_for(
                executor.rollback(request, result), timeout=self._config.rollback_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            rollback = RollbackResult(RollbackStatus.UNKNOWN, "rollback_timeout")
        except Exception:
            rollback = RollbackResult(RollbackStatus.UNKNOWN, "rollback_exception")
        if not isinstance(rollback, RollbackResult):
            rollback = RollbackResult(RollbackStatus.UNKNOWN, "rollback_result_invalid")
        if len(rollback.evidence_refs) > self._config.max_verification_evidence_refs:
            rollback = RollbackResult(RollbackStatus.UNKNOWN, "rollback_evidence_exceeded")
        self._rollback_outcomes[rollback.status.value] = (
            self._rollback_outcomes.get(rollback.status.value, 0) + 1
        )
        self._record(f"rollback_{rollback.status.value}")
        return rollback

    async def _rollback_cancelled(
        self,
        executor: ExternalActionExecutor,
        request: ActionRequest,
        result: ActionResult | None = None,
    ) -> None:
        placeholder = result or self._result(
            request, ActionStatus.CANCELLED, "cancelled",
        )
        try:
            await asyncio.shield(self._safe_rollback(executor, request, placeholder))
        except (asyncio.CancelledError, Exception):
            self._record("rollback_cancelled_unknown")

    def _with_rollback(
        self,
        result: ActionResult,
        reason: str,
        rollback: RollbackResult,
    ) -> ActionResult:
        status = result.status
        if status is ActionStatus.SUCCESS:
            status = ActionStatus.FAILED
        return ActionResult(
            schema_version=1,
            action_id=result.action_id,
            status=status,
            started_at=result.started_at,
            completed_at=result.completed_at,
            verified=False,
            verification_source=None,
            result_data={
                **dict(result.result_data),
                "world_projected": False,
                "rollback_status": rollback.status.value,
                "rollback_reason": rollback.reason_code,
            },
            error_code=reason,
        )

    def _publish_world(
        self, request: ActionRequest, evidence_refs: tuple[str, ...],
    ) -> tuple[bool, str | None]:
        try:
            event = PerceptionEvent(
                schema_version=1,
                event_id=f"action:{request.action_id}:world",
                source="runtime",
                event_type="world.observation",
                timestamp=_utc(self._clock()),
                payload={
                    "path": "stream.current_scene",
                    "value": request.target,
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

    def _find_transaction(self, key: str) -> tuple[bool, ActionTransaction | None]:
        try:
            transaction = self._transactions.find_by_idempotency_key(key)
        except Exception:
            return False, None
        if transaction is not None and not isinstance(transaction, ActionTransaction):
            return False, None
        return True, transaction

    def _transaction(self, transaction_id: str) -> ActionTransaction | None:
        try:
            value = self._transactions.get(transaction_id)
        except Exception:
            return None
        return value if isinstance(value, ActionTransaction) else None

    def _safe_release(self, transaction_id: str, reason: str) -> None:
        current = self._transaction(transaction_id)
        if current is None or current.state in {
            ActionTransactionState.COMMITTED, ActionTransactionState.RELEASED,
        }:
            return
        try:
            if self._outcome_committer is not None:
                self._outcome_committer.release(
                    self._outcome_committer.reservation_for(transaction_id), reason,
                )
            else:
                self._transactions.release(transaction_id, reason)
        except Exception:
            self._record("release_failed")

    def _commit_verified(
        self,
        transaction_id: str,
        request: ActionRequest,
        result: ActionResult,
        verification: VerificationResult,
    ) -> None:
        verified_result = ActionResult(
            schema_version=result.schema_version,
            action_id=result.action_id,
            status=ActionStatus.SUCCESS,
            started_at=result.started_at,
            completed_at=result.completed_at,
            verified=True,
            verification_source=verification.source,
            result_data=result.result_data,
            error_code=None,
        )
        reservation = self._outcome_committer.reservation_for(transaction_id)
        verified = VerifiedExecution(
            schema_version=1,
            execution_id=reservation.execution_id,
            transaction_id=transaction_id,
            request=request,
            result=verified_result,
            verification=verification,
            verified_at=_utc(self._clock()),
        )
        self._outcome_committer.commit_verified(reservation, verified)

    def _result(
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
            result_data={"world_projected": False, "rollback_status": "not_attempted"},
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
            fingerprint, request.action_type, result,
        )
        self._idempotency.move_to_end(request.idempotency_key)
        while len(self._idempotency) > self._config.max_idempotency_records:
            self._idempotency.popitem(last=False)
        return self._finish_untracked(result, outcome)

    def _finish_untracked(self, result: ActionResult, outcome: str) -> ActionResult:
        self._sequence += 1
        self._results[self._sequence] = result
        while len(self._results) > self._config.max_recent_results:
            self._results.popitem(last=False)
        self._record(outcome)
        return result

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_external_action", None)
        if callable(recorder):
            try:
                recorder("external_loop", outcome)
            except Exception:
                pass


def _executor_error(request: ActionRequest, value: object) -> str | None:
    if not isinstance(value, ActionResult):
        return "invalid_executor_result"
    if value.action_id != request.action_id:
        return "executor_action_mismatch"
    if value.verified:
        return "executor_claimed_verification"
    if value.status is not ActionStatus.SUCCESS:
        return value.error_code or "executor_failed"
    return None


def _fingerprint(request: ActionRequest) -> str:
    payload = request.to_dict()
    payload.pop("requested_at", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("external action clock must be timezone-aware")
    return value.astimezone(timezone.utc)
