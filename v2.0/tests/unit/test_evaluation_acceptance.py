from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.operations.metrics import MetricsCollector
from services.evaluation.acceptance import TextAcceptanceRunner


ROOT = Path(__file__).resolve().parents[2]


def _runner(*, enabled: bool = True, metrics=None) -> TextAcceptanceRunner:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return TextAcceptanceRunner.from_loader(loader, metrics=metrics, enabled=enabled)


def test_acceptance_passes_all_failure_and_replay_gates() -> None:
    metrics = MetricsCollector()
    report = _runner(metrics=metrics).run(seed=42)
    assert report["passed"] is True
    assert report["status"] == "passed"
    assert report["scenario_count"] == 7
    assert all(report["gates"].values())
    assert report["sanitized"] is True
    assert report["raw_transcript_included"] is False
    assert metrics.eval_acceptance_snapshot() == {"passed": 1}
    assert b'mai_eval_acceptance_runs_total{outcome="passed"} 1.0' in metrics.prometheus_text()


def test_failure_matrix_has_no_loss_false_commit_or_duplicate_delivery() -> None:
    report = _runner().run(seed=20260809)
    for row in report["results"]:
        invariants = row["observed"]["invariants"]
        assert invariants["events_received"] == invariants["events_accounted"]
        assert invariants["data_loss"] == 0
        assert invariants["false_commits"] == 0
        assert invariants["duplicate_deliveries"] == 0
        assert invariants["deadlocked"] is False
        assert invariants["decision_evidence_complete"] is True


def test_feature_disabled_cannot_be_reported_as_pass() -> None:
    metrics = MetricsCollector()
    report = _runner(enabled=False, metrics=metrics).run()
    assert report["passed"] is False
    assert report["status"] == "feature_disabled"
    assert report["scenario_count"] == 0
    assert metrics.eval_acceptance_snapshot() == {"disabled": 1}


@pytest.mark.asyncio
async def test_service_lifecycle_is_observable() -> None:
    runner = _runner()
    assert (await runner.health_check()).state.value == "stopped"
    await runner.start()
    health = await runner.health_check()
    assert health.is_ok and health.details["enabled"] is True
    await runner.stop()
    assert (await runner.health_check()).state.value == "stopped"
