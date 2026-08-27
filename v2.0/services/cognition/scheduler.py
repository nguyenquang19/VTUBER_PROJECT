"""Canonical latest-wins, single-inflight scheduler for Cognitive Brain work."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveBrainParseError,
    CognitiveBrainSchemaError,
    CognitiveBrainService,
    CognitiveBrainShadowSchedulerService,
    CognitiveBrainSnapshot,
    CognitiveContext,
    CognitiveContextBuilderService,
    CognitiveGroundingDecision,
    CognitiveGroundingGateService,
    CognitiveOpportunity,
    CognitiveModelBusyError,
    CognitiveModelContextError,
    CognitiveModelPreemptedError,
    CognitiveModelTelemetry,
    CognitiveModelTimeoutError,
    CognitiveShadowOutcome,
    CognitiveShadowRecord,
    CognitiveTurn,
)


class CognitiveOpportunityScheduler(CognitiveBrainShadowSchedulerService):
    """Bounded Brain worker whose S4 records are never consumed publicly."""

    service_id = "cognitive_brain_shadow_scheduler"

    def __init__(
        self,
        *,
        config: CognitionConfig,
        context_builder: CognitiveContextBuilderService,
        brain: CognitiveBrainService,
        grounding_gate: CognitiveGroundingGateService,
        metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._context_builder = context_builder
        self._brain = brain
        if not isinstance(grounding_gate, CognitiveGroundingGateService):
            raise ValueError("grounding_gate must implement CognitiveGroundingGateService")
        self._grounding_gate = grounding_gate
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.perf_counter
        self._running = False
        self._pending: CognitiveOpportunity | None = None
        self._pending_queued_at: datetime | None = None
        self._pending_queued_monotonic: float | None = None
        self._signal = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._active_opportunity: CognitiveOpportunity | None = None
        self._preempt_requested = False
        self._records: deque[CognitiveShadowRecord] = deque(
            maxlen=config.max_brain_shadow_records,
        )
        self._material_seen: dict[str, datetime] = {}
        self._material_completed: dict[str, datetime] = {}
        self._counts: dict[str, int] = {}
        self._live_latencies_ms: deque[float] = deque(
            maxlen=config.brain_live_latency_sample_max,
        )
        self._live_evaluated = 0
        self._live_timeouts = 0
        self._would_select = 0
        self._would_fallback = 0

    async def start(self) -> None:
        if self._running:
            return
        await self._grounding_gate.start()
        try:
            await self._brain.start()
        except BaseException:
            await self._grounding_gate.stop()
            raise
        self._running = True
        self._worker = asyncio.create_task(
            self._run(), name="cognitive-brain-shadow-scheduler",
        )

    async def stop(self) -> None:
        self._running = False
        pending = self._pending
        queued_at = self._pending_queued_at
        self._pending = None
        self._pending_queued_at = None
        self._pending_queued_monotonic = None
        if pending is not None and queued_at is not None:
            self._append_record(
                pending, CognitiveShadowOutcome.CANCELLED,
                queued_at=queued_at, started_at=None, context_id=None,
                turn=None, telemetry=None,
            )
        self._preempt_requested = False
        active = self._active_task
        if active is not None and not active.done():
            active.cancel()
        worker = self._worker
        if worker is not None:
            worker.cancel()
            try:
                await asyncio.wait_for(
                    worker, timeout=self._config.brain_cancel_grace_seconds,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._worker = None
        self._active_task = None
        self._active_opportunity = None
        self._signal.clear()
        try:
            await self._brain.stop()
        finally:
            await self._grounding_gate.stop()
        self._records.clear()
        self._material_seen.clear()
        self._material_completed.clear()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if self._worker is None or self._worker.done():
            return HealthStatus.degraded(
                self.service_id, "shadow scheduler worker is unavailable",
                queue_depth=int(self._pending is not None),
            )
        return HealthStatus.healthy(
            self.service_id,
            queue_depth=int(self._pending is not None),
            inflight_count=int(self._active_task is not None),
        )

    def get_metrics(self) -> dict[str, Any]:
        samples = tuple(self._live_latencies_ms)
        values: dict[str, Any] = {
            "cognitive_brain_shadow_running": self._running,
            "cognitive_brain_shadow_queue_depth": int(self._pending is not None),
            "cognitive_brain_shadow_inflight": int(self._active_task is not None),
            "cognitive_brain_shadow_records": len(self._records),
            "cognitive_brain_live_evaluated_total": self._live_evaluated,
            "cognitive_brain_live_latency_samples": len(samples),
            "cognitive_brain_live_latency_p50_ms": _percentile_ms(samples, 0.50),
            "cognitive_brain_live_latency_p95_ms": _percentile_ms(samples, 0.95),
            "cognitive_brain_live_timeout_total": self._live_timeouts,
            "cognitive_brain_live_timeout_rate": _rate(
                self._live_timeouts, self._live_evaluated,
            ),
            "cognitive_brain_live_would_select_total": self._would_select,
            "cognitive_brain_live_would_select_rate": _rate(
                self._would_select, self._live_evaluated,
            ),
            "cognitive_brain_live_would_fallback_total": self._would_fallback,
            "cognitive_brain_live_would_fallback_rate": _rate(
                self._would_fallback, self._live_evaluated,
            ),
        }
        for outcome, count in sorted(self._counts.items()):
            values[f"cognitive_brain_shadow_{outcome.casefold()}_total"] = count
        values.update(self._grounding_gate.get_metrics())
        return values

    def offer(self, opportunity: CognitiveOpportunity) -> bool:
        if not isinstance(opportunity, CognitiveOpportunity):
            raise TypeError("opportunity must be CognitiveOpportunity")
        now = _utc(self._clock())
        if not self._running:
            self._record_opportunity(opportunity, "blocked")
            self._append_record(
                opportunity, CognitiveShadowOutcome.SKIPPED_DISABLED,
                queued_at=now, started_at=None, context_id=None,
                turn=None, telemetry=None,
            )
            return False
        if _has_hard_hold(opportunity):
            self._record_opportunity(opportunity, "blocked")
            self._append_record(
                opportunity, CognitiveShadowOutcome.SKIPPED_HARD_HOLD,
                queued_at=now, started_at=None, context_id=None,
                turn=None, telemetry=None,
            )
            return False
        same_pending = (
            self._pending is not None
            and self._pending.material_change_ref == opportunity.material_change_ref
        )
        same_active = (
            self._active_opportunity is not None
            and self._active_opportunity.material_change_ref == opportunity.material_change_ref
        )
        seen = self._material_seen.get(opportunity.material_change_ref)
        completed = self._material_completed.get(opportunity.material_change_ref)
        duplicate = (
            (same_pending or same_active)
            and seen is not None
            and (now - seen).total_seconds() < self._config.opportunity_debounce_seconds
        )
        unchanged = (
            completed is not None
            and (now - completed).total_seconds() < self._config.opportunity_reconsider_seconds
        )
        if duplicate or unchanged:
            self._record_opportunity(opportunity, "debounced")
            self._append_record(
                opportunity, CognitiveShadowOutcome.SKIPPED_NO_CHANGE,
                queued_at=now, started_at=None, context_id=None,
                turn=None, telemetry=None,
            )
            return False

        queued_monotonic = _monotonic_seconds(self._monotonic())
        replaced = self._pending
        replaced_at = self._pending_queued_at
        if replaced is not None and replaced_at is not None:
            self._record_opportunity(replaced, "superseded")
            self._append_record(
                replaced, CognitiveShadowOutcome.SUPERSEDED,
                queued_at=replaced_at, started_at=None, context_id=None,
                turn=None, telemetry=None,
            )
        self._pending = opportunity
        self._pending_queued_at = now
        self._pending_queued_monotonic = queued_monotonic
        self._material_seen[opportunity.material_change_ref] = now
        self._trim_material(self._material_seen)
        self._observe_queue_depth()
        self._signal.set()
        self._record_opportunity(opportunity, "offered")
        return True

    def preempt_for_live(self) -> None:
        task = self._active_task
        if task is None or task.done():
            return
        self._preempt_requested = True
        task.cancel()

    def recent(self, limit: int | None = None) -> tuple[CognitiveShadowRecord, ...]:
        records = tuple(self._records)
        if limit is None:
            return records
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        return records[-limit:] if limit else ()

    def snapshot(self) -> CognitiveBrainSnapshot:
        records = tuple(self._records)
        return CognitiveBrainSnapshot(
            config=self._config,
            schema_version=self._config.schema_version,
            running=self._running,
            healthy=self._running and self._worker is not None and not self._worker.done(),
            queue_depth=int(self._pending is not None),
            inflight_count=int(self._active_task is not None),
            retained_record_count=len(records),
            last_outcome=records[-1].outcome if records else None,
            recent_records=records,
        )

    async def _run(self) -> None:
        while self._running:
            await self._signal.wait()
            self._signal.clear()
            while self._running and self._pending is not None:
                opportunity = self._pending
                queued_at = self._pending_queued_at or _utc(self._clock())
                queued_monotonic = (
                    self._pending_queued_monotonic
                    if self._pending_queued_monotonic is not None
                    else _monotonic_seconds(self._monotonic())
                )
                self._pending = None
                self._pending_queued_at = None
                self._pending_queued_monotonic = None
                self._observe_queue_depth()
                self._active_opportunity = opportunity
                self._preempt_requested = False
                task = asyncio.create_task(
                    self._process(opportunity, queued_at, queued_monotonic),
                )
                self._active_task = task
                try:
                    await task
                except asyncio.CancelledError:
                    outcome = (
                        CognitiveShadowOutcome.PREEMPTED
                        if self._preempt_requested else CognitiveShadowOutcome.CANCELLED
                    )
                    self._append_record(
                        opportunity, outcome, queued_at=queued_at,
                        started_at=None, context_id=None, turn=None,
                        telemetry=_telemetry(self._brain),
                        live_latency_ms=(
                            max(
                                0.0,
                                (
                                    _monotonic_seconds(self._monotonic())
                                    - queued_monotonic
                                ) * 1000.0,
                            )
                            if outcome is CognitiveShadowOutcome.PREEMPTED
                            else None
                        ),
                    )
                    if not self._running:
                        raise
                finally:
                    self._active_task = None
                    self._active_opportunity = None
                    self._preempt_requested = False

    async def _process(
        self,
        opportunity: CognitiveOpportunity,
        queued_at: datetime,
        queued_monotonic: float,
    ) -> None:
        started_at = _utc(self._clock())
        context: CognitiveContext | None = None
        grounding_decision: CognitiveGroundingDecision | None = None
        turn: CognitiveTurn | None = None
        result: tuple[CognitiveContext, CognitiveGroundingDecision] | None = None
        outcome = CognitiveShadowOutcome.SERVICE_ERROR

        def live_latency_ms() -> float:
            return max(
                0.0,
                (_monotonic_seconds(self._monotonic()) - queued_monotonic) * 1000.0,
            )

        if (
            started_at - opportunity.opened_at
        ).total_seconds() > self._config.max_opportunity_age_seconds:
            self._append_record(
                opportunity, CognitiveShadowOutcome.STALE,
                queued_at=queued_at, started_at=started_at,
                context_id=None, turn=None, telemetry=None,
                live_latency_ms=live_latency_ms(),
            )
            return

        async def build_grounded() -> tuple[
            CognitiveContext, CognitiveGroundingDecision,
        ] | None:
            nonlocal context
            context = await self._context_builder.build(opportunity.context_request)
            if context is None:
                return None
            source_turn = await self._brain.propose(context)
            decision = self._grounding_gate.evaluate(context, source_turn)
            return context, decision

        elapsed_seconds = live_latency_ms() / 1000.0
        remaining_seconds = self._config.brain_live_timeout_seconds - elapsed_seconds
        if remaining_seconds <= 0:
            outcome = CognitiveShadowOutcome.TIMEOUT
        else:
            try:
                result = await asyncio.wait_for(
                    build_grounded(), timeout=remaining_seconds,
                )
            except CognitiveModelBusyError:
                outcome = CognitiveShadowOutcome.SKIPPED_BUSY
            except CognitiveModelContextError:
                outcome = CognitiveShadowOutcome.PREFLIGHT_REJECTED
            except CognitiveModelPreemptedError:
                outcome = CognitiveShadowOutcome.PREEMPTED
            except (CognitiveModelTimeoutError, asyncio.TimeoutError):
                outcome = CognitiveShadowOutcome.TIMEOUT
            except CognitiveBrainParseError:
                outcome = CognitiveShadowOutcome.PARSE_REJECTED
            except (CognitiveBrainSchemaError, ValueError):
                outcome = CognitiveShadowOutcome.SCHEMA_REJECTED
            except asyncio.CancelledError:
                raise
            except Exception:
                outcome = CognitiveShadowOutcome.SERVICE_ERROR
            else:
                if result is None:
                    outcome = CognitiveShadowOutcome.SERVICE_ERROR
                else:
                    context, grounding_decision = result
                    turn = grounding_decision.effective_turn
        if turn is not None and grounding_decision is not None and context is not None:
            completed_at = _utc(self._clock())
            if (
                completed_at - opportunity.opened_at
            ).total_seconds() > self._config.max_opportunity_age_seconds:
                self._append_record(
                    opportunity, CognitiveShadowOutcome.STALE,
                    queued_at=queued_at, started_at=started_at,
                    context_id=context.context_id, turn=None,
                    telemetry=_telemetry(self._brain),
                    live_latency_ms=live_latency_ms(),
                )
                return
            self._append_record(
                opportunity, CognitiveShadowOutcome.PROPOSED,
                queued_at=queued_at, started_at=started_at,
                context_id=context.context_id, turn=turn,
                telemetry=_telemetry(self._brain),
                grounding_decision=grounding_decision,
                live_latency_ms=live_latency_ms(),
            )
            return
        self._append_record(
            opportunity, outcome, queued_at=queued_at,
            started_at=started_at,
            context_id=None if context is None else context.context_id,
            turn=None, telemetry=_telemetry(self._brain),
            live_latency_ms=live_latency_ms(),
        )

    def _append_record(
        self,
        opportunity: CognitiveOpportunity,
        outcome: CognitiveShadowOutcome,
        *,
        queued_at: datetime,
        started_at: datetime | None,
        context_id: str | None,
        turn: Any,
        telemetry: CognitiveModelTelemetry | None,
        grounding_decision: CognitiveGroundingDecision | None = None,
        live_latency_ms: float | None = None,
    ) -> None:
        completed_at = _utc(self._clock())
        if completed_at < queued_at:
            completed_at = queued_at
        if started_at is not None and started_at < queued_at:
            started_at = queued_at
        if started_at is not None and completed_at < started_at:
            completed_at = started_at
        queue_wait = None if started_at is None else (
            started_at - queued_at
        ).total_seconds() * 1000.0
        record_id = "shadow-record:" + hashlib.sha256(
            f"{opportunity.opportunity_id}\n{outcome.value}".encode("utf-8")
        ).hexdigest()[:64]
        record = CognitiveShadowRecord(
            config=self._config,
            schema_version=self._config.schema_version,
            record_id=record_id,
            opportunity_id=opportunity.opportunity_id,
            context_id=context_id,
            compatibility=opportunity.compatibility,
            outcome=outcome,
            turn=turn,
            grounding_decision=grounding_decision,
            queued_at=queued_at,
            started_at=started_at,
            completed_at=completed_at,
            queue_wait_ms=queue_wait,
            ttft_ms=None if telemetry is None else telemetry.ttft_ms,
            generation_ms=None if telemetry is None else telemetry.generation_ms,
            input_tokens=None if telemetry is None else telemetry.input_tokens,
            output_tokens=None if telemetry is None else telemetry.output_tokens,
        )
        self._records.append(record)
        if started_at is not None:
            self._material_completed[opportunity.material_change_ref] = completed_at
            self._trim_material(self._material_completed)
        self._counts[outcome.value] = self._counts.get(outcome.value, 0) + 1
        if live_latency_ms is not None:
            self._record_live_result(
                latency_ms=live_latency_ms,
                would_select=outcome is CognitiveShadowOutcome.PROPOSED,
                timed_out=outcome is CognitiveShadowOutcome.TIMEOUT,
            )
        _call_metric(self._metrics, "record_cognitive_brain_request", outcome.value)
        if turn is not None:
            _call_metric(self._metrics, "record_cognitive_brain_turn", turn.mode.value)
        if queue_wait is not None:
            _call_metric(
                self._metrics, "observe_cognitive_brain_queue_wait", queue_wait / 1000.0,
            )
        if telemetry is not None:
            _call_metric(
                self._metrics, "observe_cognitive_brain_generation",
                outcome.value, telemetry.generation_ms / 1000.0,
            )
            if telemetry.ttft_ms is not None:
                _call_metric(
                    self._metrics, "observe_cognitive_brain_ttft",
                    outcome.value, telemetry.ttft_ms / 1000.0,
                )
            if telemetry.input_tokens is not None:
                _call_metric(
                    self._metrics, "observe_cognitive_brain_tokens",
                    telemetry.input_tokens, telemetry.output_tokens,
                )

    def _record_opportunity(
        self, opportunity: CognitiveOpportunity, outcome: str,
    ) -> None:
        _call_metric(
            self._metrics, "record_cognitive_brain_opportunity",
            opportunity.kind.value, outcome,
        )

    def _trim_material(self, values: dict[str, datetime]) -> None:
        while len(values) > self._config.max_brain_shadow_records:
            oldest = next(iter(values))
            values.pop(oldest, None)

    def _observe_queue_depth(self) -> None:
        _call_metric(
            self._metrics, "set_cognitive_brain_queue_depth",
            int(self._pending is not None),
        )

    def _record_live_result(
        self, *, latency_ms: float, would_select: bool, timed_out: bool,
    ) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("live Brain latency must be finite and non-negative")
        self._live_latencies_ms.append(latency_ms)
        self._live_evaluated += 1
        if would_select:
            self._would_select += 1
            result = "would_select"
        else:
            self._would_fallback += 1
            result = "would_fallback"
        if timed_out:
            self._live_timeouts += 1
        _call_metric(
            self._metrics, "observe_cognitive_brain_live_latency",
            latency_ms / 1000.0,
        )
        _call_metric(
            self._metrics, "record_cognitive_brain_live_result", result,
        )
        if timed_out:
            _call_metric(self._metrics, "record_cognitive_brain_live_timeout")


def _has_hard_hold(opportunity: CognitiveOpportunity) -> bool:
    state = opportunity.context_request.hard_state
    return any((
        state.emergency, state.operator_hold, state.safety_hold,
        state.permission_hold, state.transaction_conflict, state.critical_state,
    ))


def _telemetry(brain: CognitiveBrainService) -> CognitiveModelTelemetry | None:
    value = getattr(brain, "last_telemetry", None)
    return value if isinstance(value, CognitiveModelTelemetry) else None


def _call_metric(metrics: Any, method: str, *args: Any) -> None:
    recorder = getattr(metrics, method, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _monotonic_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("scheduler monotonic clock must return a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("scheduler monotonic clock must be finite and non-negative")
    return result


def _percentile_ms(values: tuple[float, ...], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0
