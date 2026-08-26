from __future__ import annotations

from pathlib import Path

import pytest

from services.evaluation.scenario_loader import load_scenario_suite
from interfaces.evaluation import HumanRubric, ObservedOutcome, ScenarioGroup


ROOT = Path(__file__).resolve().parents[2]


def test_frozen_suite_covers_every_group_and_has_unique_ids() -> None:
    suite = load_scenario_suite(ROOT / "eval" / "scenarios" / "mai_agent_v1.yaml")
    assert suite.contract_id == "mai-agent-v1"
    assert {item.group for item in suite.scenarios} == set(ScenarioGroup)
    assert len({item.scenario_id for item in suite.scenarios}) == len(suite.scenarios) == 19


def test_loader_rejects_unknown_root_schema(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "schema_version: 1\ncontract_id: x\nscenarios: []\nunknown: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="root schema"):
        load_scenario_suite(path)


def test_evaluation_value_objects_reject_empty_evidence_fields() -> None:
    with pytest.raises(ValueError, match="human rubric"):
        HumanRubric(dimension="", instruction="Review it")
    with pytest.raises(ValueError, match="scenario id"):
        ObservedOutcome(scenario_id=" ")
