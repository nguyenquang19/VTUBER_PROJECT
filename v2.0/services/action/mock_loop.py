"""Bounded generic action coordinator for the Phase 5 mock-only closed loop."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.action_execution import ActionExecutor, ActionVerifier, GeneralActionService
from interfaces.base import HealthStatus
from interfaces.compatibility import (
    ActionRequest, ActionResult, ActionStatus, EventProvenance, PerceptionEvent,
)


@dataclass(frozen=True)
class ActionMockConfig:
    execution_timeout_s: float
    max_recent_results: int
    default_outcome: str

    @classmethod
    def from_loader(cls, loader: Any) -> "ActionMockConfig":
        raw = loader.get("capabilities", "mock_action", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("mock_action config must be a mapping")
        config = cls(
            execution_timeout_s=float(raw.get("execution_timeout_s", 0)),
            max_recent_results=int(raw.get("max_recent_results", 0)),
            default_outcome=str(raw.get("default_outcome", "")).strip().lower(),
        )
        if (
            config.execution_timeout_s <= 0 or config.max_recent_results <= 0
            or config.default_outcome not in {"success", "failed"}
        ):
            raise ValueError("mock action config is invalid")
        return config


class GeneralActionMockLoop(GeneralActionService):
    """Generic closed loop that commits only after verification and World update."""

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
        self._config = config
        self._registry = capability_registry
        self._transactions = transactions
        self._world_model = world_model
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._executors: dict[str, ActionExecutor] = {}
        self._verifiers: dict[str, ActionVerifier] = {}
        self._results: OrderedDict[str, ActionResult] = OrderedDict()
        self._lock = asyncio.Lock()
        self._running = False
        self._outcomes: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "GeneralActionMockLoop":
        return cls(ActionMockConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def register_executor(self, executor_id: str, executor: ActionExecutor) -> None:
        self._executors[str(executor_id).strip()] = executor

    def register_verifier(self, verifier_id: str, verifier: ActionVerifier) -> None:
        self._verifiers[str(verifier_id).strip()] = verifier

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
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "recent": [result.to_dict() for result in self._results.values()],
        }

    async def execute(self, request: ActionRequest) -> ActionResult:
        async with self._lock:
            cached = self._results.get(request.idempotency_key)
            if cached is not None:
                self._record("duplicate")
                return cached
            return await self._execute_once(request)

    async def _execute_once(self, request: ActionRequest) -> ActionResult:
        rejected = self._validate(request)
        if rejected is not None:
            return self._store(request, rejected)
        capability = self._registry.capability(request.capability_id)
        assert capability is not None
        executor = self._executors[capability.executor_id]
        verifier = self._verifiers[capability.verifier_id]
        reservation = self._transactions.reserve(request.action_type, request.idempotency_key)
        if not reservation.created:
            prior = self._results.get(request.idempotency_key)
            if prior is not None:
                self._record("duplicate")
                return prior
            return self._store(request, self._rejected(request, "duplicate_result_unavailable"))
        transaction_id = reservation.transaction.transaction_id
        try:
            self._transactions.mark_generated(transaction_id)
            self._transactions.mark_delivering(transaction_id)
            result = await asyncio.wait_for(
                executor.execute(request), timeout=self._config.execution_timeout_s,
            )
        except asyncio.TimeoutError:
            self._transactions.release(transaction_id, "execution_timeout")
            return self._store(request, self._failed(request, ActionStatus.TIMEOUT, "execution_timeout"))
        except Exception as exc:
            self._transactions.release(transaction_id, "executor_exception")
            return self._store(request, self._failed(request, ActionStatus.FAILED, f"executor_{type(exc).__name__.lower()}"))
        if result.action_id != request.action_id or result.status is not ActionStatus.SUCCESS:
            self._transactions.release(transaction_id, "executor_failed")
            return self._store(request, self._failed(request, ActionStatus.FAILED, result.error_code or "executor_failed"))
        try:
            verification = await asyncio.wait_for(
                verifier.verify(request, result), timeout=self._config.execution_timeout_s,
            )
        except (asyncio.TimeoutError, Exception):
            self._transactions.release(transaction_id, "verification_unknown")
            return self._store(request, self._failed(request, ActionStatus.FAILED, "verification_unknown"))
        if not verification.verified:
            self._transactions.release(transaction_id, verification.reason_code)
            return self._store(request, self._failed(request, ActionStatus.FAILED, verification.reason_code))
        if not self._publish_world(request, verification.evidence_refs):
            self._transactions.release(transaction_id, "world_update_failed")
            return self._store(request, self._failed(request, ActionStatus.FAILED, "world_update_failed"))
        self._transactions.mark_delivered(transaction_id)
        self._transactions.commit(transaction_id)
        verified = ActionResult(
            schema_version=1,
            action_id=result.action_id,
            status=ActionStatus.SUCCESS,
            started_at=result.started_at,
            completed_at=_utc(self._clock()),
            verified=True,
            verification_source=verification.source,
            result_data={**dict(result.result_data), "mock_only": True},
        )
        self._record("verified")
        return self._store(request, verified)

    def _validate(self, request: ActionRequest) -> ActionResult | None:
        if not self._enabled:
            return self._rejected(request, "feature_disabled")
        if request.schema_version != 1:
            return self._rejected(request, "unsupported_schema")
        capability = self._registry.capability(request.capability_id)
        if capability is None:
            return self._rejected(request, "unknown_capability")
        if request.action_type != capability.action_type or request.transaction_policy != capability.transaction_policy:
            return self._rejected(request, "capability_mismatch")
        availability = self._registry.availability(request.capability_id)
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

    def _publish_world(self, request: ActionRequest, evidence_refs: tuple[str, ...]) -> bool:
        if request.action_type == "CALL_GUEST":
            value = True
        elif request.action_type == "REMOVE_GUEST":
            value = False
        else:
            return False
        event = PerceptionEvent(
            schema_version=1,
            event_id=f"action:{request.action_id}:world",
            source="runtime",
            event_type="world.observation",
            timestamp=_utc(self._clock()),
            payload={"path": "call.guest_connected", "value": value, "evidence_refs": list(evidence_refs)},
            provenance=EventProvenance(
                producer=self.service_id, source_event_id=request.action_id,
            ),
            confidence=1.0,
        )
        try:
            return bool(self._world_model.apply_event(event))
        except Exception:
            return False

    def _rejected(self, request: ActionRequest, reason: str) -> ActionResult:
        self._record("rejected")
        return self._failed(request, ActionStatus.REJECTED, reason, record=False)

    def _failed(
        self, request: ActionRequest, status: ActionStatus, reason: str,
        *, record: bool = True,
    ) -> ActionResult:
        if record:
            self._record("released")
        now = _utc(self._clock())
        return ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=status,
            started_at=now,
            completed_at=now,
            verified=False,
            verification_source=None,
            result_data={"mock_only": True},
            error_code=reason,
        )

    def _store(self, request: ActionRequest, result: ActionResult) -> ActionResult:
        self._results[request.idempotency_key] = result
        while len(self._results) > self._config.max_recent_results:
            self._results.popitem(last=False)
        return result

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_action_mock_outcome"):
            self._metrics.record_action_mock_outcome(outcome)


def _arguments_match(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    if set(arguments) != set(schema):
        return not arguments and not schema
    return all(
        (expected == "string" and isinstance(arguments.get(name), str) and bool(arguments[name].strip()))
        for name, expected in schema.items()
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("mock action clock must be timezone-aware")
    return value.astimezone(timezone.utc)
