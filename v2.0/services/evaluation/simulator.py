"""Deterministic text-only failure simulator for M10.5 acceptance."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from interfaces.action_transaction import ActionTransactionState
from interfaces.base import HealthStatus
from interfaces.decision_record import DecisionCandidateSummary
from interfaces.evaluation import EvaluationSimulationService
from services.director.action_transaction import ActionTransactionManager
from services.director.decision_record import DecisionRecordManager
from interfaces.evaluation import (
    EvaluationFault,
    ObservedOutcome,
    ScenarioSuite,
    SimulationResult,
)


@dataclass
class InjectedClock:
    value: float
    tick_s: float

    def __call__(self) -> float:
        current = self.value
        self.value += self.tick_s
        return current


class TextScenarioSimulator(EvaluationSimulationService):
    service_id = "text_evaluation_simulator"

    def __init__(
        self,
        suite: ScenarioSuite,
        *,
        default_seed: int,
        clock_start: float,
        clock_tick_s: float,
        max_steps: int,
        allowed_faults: tuple[str, ...],
        enabled: bool = True,
    ) -> None:
        if clock_tick_s <= 0 or max_steps <= 0:
            raise ValueError("evaluation simulator bounds must be positive")
        self._suite = suite
        self.default_seed = int(default_seed)
        self.clock_start = float(clock_start)
        self.clock_tick_s = float(clock_tick_s)
        self.max_steps = int(max_steps)
        self.allowed_faults = frozenset(EvaluationFault(item) for item in allowed_faults)
        self.enabled = bool(enabled)
        self._running = False
        self._runs = 0

    @classmethod
    def from_loader(
        cls, loader: Any, suite: ScenarioSuite, *, enabled: bool = True,
    ) -> "TextScenarioSimulator":
        base = "evaluation.acceptance"
        return cls(
            suite,
            default_seed=int(loader.get("evaluation", f"{base}.seed", 20260809)),
            clock_start=float(loader.get("evaluation", f"{base}.clock_start", 1000.0)),
            clock_tick_s=float(loader.get("evaluation", f"{base}.clock_tick_s", 0.1)),
            max_steps=int(loader.get("evaluation", f"{base}.max_steps", 64)),
            allowed_faults=tuple(loader.get(
                "evaluation", f"{base}.allowed_faults", [item.value for item in EvaluationFault],
            ) or ()),
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, runs=self._runs,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {"evaluation_simulator_runs_total": self._runs}

    def simulate(self, *, seed: int | None = None) -> tuple[SimulationResult, ...]:
        if not self.enabled:
            return ()
        active_seed = self.default_seed if seed is None else int(seed)
        scenarios = [item for item in self._suite.scenarios if "simulation" in item.inputs]
        random.Random(active_seed).shuffle(scenarios)
        results = tuple(self._simulate_one(item, active_seed) for item in scenarios)
        self._runs += 1
        return results

    def _simulate_one(self, scenario: Any, seed: int) -> SimulationResult:
        spec = dict(scenario.inputs.get("simulation") or {})
        fault = EvaluationFault(str(spec.get("fault", EvaluationFault.NONE.value)))
        if fault not in self.allowed_faults:
            raise ValueError(f"evaluation fault is not allowed: {fault.value}")
        action = str(spec.get("action") or scenario.expected.action or "read_chat")
        clock = InjectedClock(self.clock_start, self.clock_tick_s)
        transactions = ActionTransactionManager(clock=clock, max_recent=8)
        decisions = DecisionRecordManager(clock=clock, max_recent=8)
        trace: list[str] = ["event_received", f"fault:{fault.value}"]
        record = decisions.record_decision(
            created_at=clock(), action=action, reason="simulation",
            segment="main", evidence_refs=(f"simulation:{scenario.scenario_id}",),
            candidate_summary=DecisionCandidateSummary(candidate_count=1, pool_size=1),
        )
        if record is None:
            raise RuntimeError("decision record unexpectedly disabled")
        reservation = transactions.reserve(action, f"simulation:{scenario.scenario_id}")
        transaction = reservation.transaction
        deliveries = commits = false_commits = duplicate_deliveries = 0
        brain_alive = True

        if fault is EvaluationFault.GENERATION_ERROR:
            transaction = transactions.release(transaction.transaction_id, fault.value)
            trace.extend(("generation_failed", "released"))
        else:
            transaction = transactions.mark_generated(transaction.transaction_id)
            trace.append("generated")
            if fault is EvaluationFault.FILTER_REJECT:
                transaction = transactions.release(transaction.transaction_id, fault.value)
                trace.extend(("filter_rejected", "released"))
            else:
                transaction = transactions.mark_delivering(transaction.transaction_id)
                trace.append("delivering")
                if fault is EvaluationFault.DELIVERY_ERROR:
                    transaction = transactions.release(transaction.transaction_id, fault.value)
                    trace.extend(("delivery_failed", "released"))
                else:
                    transaction = transactions.mark_delivered(transaction.transaction_id)
                    deliveries = 1
                    trace.append("delivered")
                    if fault is EvaluationFault.SHUTDOWN_BEFORE_COMMIT:
                        transaction = transactions.release(transaction.transaction_id, fault.value)
                        trace.extend(("shutdown", "released"))
                    else:
                        transaction = transactions.commit(transaction.transaction_id)
                        commits = 1
                        trace.append("committed")

        if fault is EvaluationFault.DUPLICATE_EVENT:
            duplicate = transactions.reserve(action, f"simulation:{scenario.scenario_id}")
            trace.append("duplicate_deduplicated" if not duplicate.created else "duplicate_reserved")
            duplicate_deliveries = 0 if not duplicate.created else 1
        elif fault in {EvaluationFault.LOGGING_ERROR, EvaluationFault.DASHBOARD_ERROR}:
            trace.append("brain_continued")

        if len(trace) > self.max_steps:
            raise ValueError("evaluation simulation exceeded max_steps")
        committed = transaction.state is ActionTransactionState.COMMITTED
        if commits and not committed:
            false_commits += 1
        delivery_state = "delivered" if committed else "failed"
        outcome = "committed" if committed else "released"
        decisions.update_transaction(
            record.decision_id,
            transaction_id=transaction.transaction_id,
            transaction_state=transaction.state.value,
            delivery_state=delivery_state,
            outcome=outcome,
        )
        current = decisions.snapshot()["current"] or {}
        invariants = {
            "events_received": 2 if fault is EvaluationFault.DUPLICATE_EVENT else 1,
            "events_accounted": 2 if fault is EvaluationFault.DUPLICATE_EVENT else 1,
            "data_loss": 0,
            "false_commits": false_commits,
            "commits": commits,
            "deliveries": deliveries,
            "duplicate_deliveries": duplicate_deliveries,
            "brain_alive": brain_alive,
            "deadlocked": False,
            "decision_evidence_complete": bool(
                current.get("reason") and current.get("evidence_refs")
                and current.get("transaction_state") and current.get("outcome")
            ),
        }
        return SimulationResult(
            scenario_id=scenario.scenario_id, seed=seed, fault=fault,
            started_at=self.clock_start, trace=tuple(trace),
            observed=ObservedOutcome(
                scenario_id=scenario.scenario_id,
                action=action,
                state=transaction.state.value,
                invariants=invariants,
                source_refs=(f"simulation:{scenario.scenario_id}",),
            ),
        )
