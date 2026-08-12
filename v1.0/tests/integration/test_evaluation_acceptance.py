from __future__ import annotations

import json
from pathlib import Path

from scripts.run_agent_eval import main
from orchestrator.config_loader import ConfigLoader
from services.evaluation.acceptance import TextAcceptanceRunner


ROOT = Path(__file__).resolve().parents[2]


def test_cli_runs_reproducible_text_acceptance_with_one_command(
    tmp_path: Path, capsys,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert main(["--acceptance", "--seed", "77", "--output", str(first)]) == 0
    first_summary = json.loads(capsys.readouterr().out)
    assert main(["--acceptance", "--seed", "77", "--output", str(second)]) == 0
    capsys.readouterr()
    first_report = json.loads(first.read_text(encoding="utf-8"))
    second_report = json.loads(second.read_text(encoding="utf-8"))
    assert first_report == second_report
    assert first_summary["passed"] is True
    assert first_summary["scenario_count"] == 7


def test_acceptance_artifact_contains_delivery_aware_failure_evidence(
    tmp_path: Path, capsys,
) -> None:
    output = tmp_path / "acceptance.json"
    assert main(["--acceptance", "--output", str(output)]) == 0
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    rows = {item["fault"]: item for item in report["results"]}
    assert rows["delivery_error"]["observed"]["state"] == "released"
    assert rows["shutdown_before_commit"]["observed"]["invariants"]["commits"] == 0
    assert rows["duplicate_event"]["observed"]["invariants"]["duplicate_deliveries"] == 0
    assert all(item["evaluation"]["outcome"] == "passed" for item in rows.values())


def test_committed_baseline_replays_exactly() -> None:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    runner = TextAcceptanceRunner.from_loader(loader)
    baseline = json.loads(
        (ROOT / "docs" / "baselines" / "m10_text_acceptance.json").read_text(
            encoding="utf-8",
        )
    )
    assert runner.run(seed=baseline["seed"]) == baseline
