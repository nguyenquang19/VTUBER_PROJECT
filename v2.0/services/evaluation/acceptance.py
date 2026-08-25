"""Text-only M10.5 acceptance gates built on the existing M8 harness."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.evaluation import EvaluationAcceptanceService
from services.evaluation.harness import ScenarioEvaluationHarness
from services.evaluation.simulator import TextScenarioSimulator
from interfaces.evaluation import EvalOutcome


class TextAcceptanceRunner(EvaluationAcceptanceService):
    service_id = "text_evaluation_acceptance"

    def __init__(
        self,
        harness: ScenarioEvaluationHarness,
        simulator: TextScenarioSimulator,
        *,
        min_failure_scenarios: int,
        artifact_file: Path,
        metrics: Any = None,
        enabled: bool = True,
    ) -> None:
        if min_failure_scenarios <= 0:
            raise ValueError("min_failure_scenarios must be positive")
        self.harness = harness
        self.simulator = simulator
        self.min_failure_scenarios = int(min_failure_scenarios)
        self.artifact_file = Path(artifact_file)
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._runs = 0
        self._last_status = "not_run"

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        metrics: Any = None,
        enabled: bool = True,
    ) -> "TextAcceptanceRunner":
        harness = ScenarioEvaluationHarness.from_loader(
            loader, metrics=metrics, enabled=enabled,
        )
        simulator = TextScenarioSimulator.from_loader(loader, harness.suite(), enabled=enabled)
        base = "evaluation.acceptance"
        return cls(
            harness,
            simulator,
            min_failure_scenarios=int(loader.get(
                "evaluation", f"{base}.min_failure_scenarios", 7,
            )),
            artifact_file=Path(loader.get(
                "evaluation", f"{base}.artifact_file",
                "logs/evaluation/m10_text_acceptance.json",
            )),
            metrics=metrics,
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True
        await self.harness.start()
        await self.simulator.start()

    async def stop(self) -> None:
        await self.simulator.stop()
        await self.harness.stop()
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, runs=self._runs,
            last_status=self._last_status,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "evaluation_acceptance_runs_total": self._runs,
            "evaluation_acceptance_last_status": self._last_status,
        }

    def run(self, *, seed: int | None = None) -> dict[str, Any]:
        if not self.enabled:
            report = self._disabled_report(seed)
            self._finish("disabled")
            return report
        first = self.simulator.simulate(seed=seed)
        replay = self.simulator.simulate(seed=seed)
        deterministic = tuple(item.replay_key() for item in first) == tuple(
            item.replay_key() for item in replay
        )
        scenario_results = self.harness.evaluate_many(tuple(item.observed for item in first))
        invariants = [dict(item.observed.invariants) for item in first]
        gates = {
            "failure_corpus": len(first) >= self.min_failure_scenarios,
            "all_scenarios_observed": bool(scenario_results) and all(
                item.outcome is not EvalOutcome.NOT_OBSERVED for item in scenario_results
            ),
            "all_contract_checks": bool(scenario_results) and all(
                item.outcome is EvalOutcome.PASSED for item in scenario_results
            ),
            "no_data_loss": bool(invariants) and all(
                row.get("data_loss") == 0
                and row.get("events_received") == row.get("events_accounted")
                for row in invariants
            ),
            "no_false_commit": bool(invariants) and all(
                row.get("false_commits") == 0 for row in invariants
            ),
            "no_deadlock": bool(invariants) and all(
                row.get("deadlocked") is False for row in invariants
            ),
            "decision_evidence": bool(invariants) and all(
                row.get("decision_evidence_complete") is True for row in invariants
            ),
            "deterministic_replay": deterministic,
        }
        passed = all(gates.values())
        status = "passed" if passed else "failed"
        by_id = {item.scenario_id: item for item in scenario_results}
        report = {
            "schema_version": 1,
            "milestone": "M10.5",
            "contract_id": self.harness.suite().contract_id,
            "workload": "deterministic_text_failure_simulation",
            "seed": first[0].seed if first else (
                self.simulator.default_seed if seed is None else int(seed)
            ),
            "sanitized": True,
            "raw_transcript_included": False,
            "scenario_count": len(first),
            "gates": gates,
            "passed": passed,
            "status": status,
            "results": [
                {
                    **item.to_dict(),
                    "evaluation": by_id[item.scenario_id].to_dict(),
                }
                for item in first
            ],
        }
        self._finish(status)
        return report

    def _disabled_report(self, seed: int | None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "milestone": "M10.5",
            "contract_id": self.harness.suite().contract_id,
            "workload": "deterministic_text_failure_simulation",
            "seed": self.simulator.default_seed if seed is None else int(seed),
            "sanitized": True,
            "raw_transcript_included": False,
            "scenario_count": 0,
            "gates": {},
            "passed": False,
            "status": "feature_disabled",
            "results": [],
        }

    def _finish(self, status: str) -> None:
        self._runs += 1
        self._last_status = str(status)
        if self._metrics is not None and hasattr(
            self._metrics, "record_eval_acceptance_run",
        ):
            try:
                self._metrics.record_eval_acceptance_run(status)
            except Exception:
                pass
