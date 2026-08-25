"""Evidence-backed scenario comparison harness (M8.1)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.evaluation import EvaluationService
from services.evaluation.scenario_loader import load_scenario_suite
from interfaces.evaluation import (
    EvalOutcome,
    ObservedOutcome,
    ScenarioResult,
    ScenarioSuite,
)


class ScenarioEvaluationHarness(EvaluationService):
    service_id = "scenario_evaluation_harness"

    def __init__(self, suite: ScenarioSuite, *, metrics: Any = None, enabled: bool = True) -> None:
        self._suite = suite
        self._by_id = {item.scenario_id: item for item in suite.scenarios}
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._running = False

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None, enabled: bool = True) -> "ScenarioEvaluationHarness":
        path = Path(loader.get("evaluation", "evaluation.scenario_file", "eval/scenarios/mai_agent_v1.yaml"))
        return cls(load_scenario_suite(path), metrics=metrics, enabled=enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, contract_id=self._suite.contract_id,
            scenario_count=len(self._suite.scenarios), enabled=self._enabled,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "evaluation_scenario_count": len(self._suite.scenarios),
            "evaluation_enabled": self._enabled,
        }

    def suite(self) -> ScenarioSuite:
        return self._suite

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def evaluate(self, observed: ObservedOutcome) -> ScenarioResult:
        scenario = self._by_id.get(observed.scenario_id)
        if scenario is None:
            raise KeyError(f"unknown scenario: {observed.scenario_id}")
        if not self._enabled or not observed.source_refs:
            result = ScenarioResult(
                scenario_id=scenario.scenario_id, group=scenario.group,
                outcome=EvalOutcome.NOT_OBSERVED, checks={}, source_refs=(),
                human_review_required=bool(scenario.human_rubric),
                reason="feature_disabled" if not self._enabled else "missing_source_refs",
            )
            self._record(result)
            return result
        checks: dict[str, bool] = {}
        if scenario.expected.action is not None:
            checks["action"] = observed.action == scenario.expected.action
        if scenario.expected.state is not None:
            checks["state"] = observed.state == scenario.expected.state
        for key, expected in scenario.expected.invariants.items():
            checks[f"invariant:{key}"] = observed.invariants.get(key) == expected
        outcome = EvalOutcome.PASSED if checks and all(checks.values()) else EvalOutcome.FAILED
        result = ScenarioResult(
            scenario_id=scenario.scenario_id, group=scenario.group, outcome=outcome,
            checks=checks, source_refs=observed.source_refs,
            human_review_required=bool(scenario.human_rubric),
            reason="" if outcome is EvalOutcome.PASSED else "expected_outcome_mismatch",
        )
        self._record(result)
        return result

    def evaluate_many(
        self, observed: tuple[ObservedOutcome, ...],
    ) -> tuple[ScenarioResult, ...]:
        return tuple(self.evaluate(item) for item in observed)

    def _record(self, result: ScenarioResult) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_eval_scenario"):
            self._metrics.record_eval_scenario(result.group.value, result.outcome.value)

