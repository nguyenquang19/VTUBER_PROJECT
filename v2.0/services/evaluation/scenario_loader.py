"""Strict YAML loader for versioned M8 scenario suites."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from interfaces.evaluation import (
    EvaluationScenario,
    ExpectedOutcome,
    HumanRubric,
    ScenarioGroup,
    ScenarioSuite,
)

_ROOT_KEYS = {"schema_version", "contract_id", "scenarios"}
_SCENARIO_KEYS = {"id", "version", "group", "description", "inputs", "expected", "human_rubric"}
_EXPECTED_KEYS = {"action", "state", "invariants"}
_RUBRIC_KEYS = {"dimension", "instruction", "required"}


def load_scenario_suite(path: str | Path) -> ScenarioSuite:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != _ROOT_KEYS:
        raise ValueError("scenario suite root schema is invalid")
    raw_scenarios = data.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("scenario suite requires a non-empty scenarios list")
    scenarios = tuple(_scenario(item) for item in raw_scenarios)
    suite = ScenarioSuite(
        schema_version=int(data["schema_version"]),
        contract_id=str(data["contract_id"]), scenarios=scenarios,
    )
    missing = set(ScenarioGroup) - {item.group for item in suite.scenarios}
    if missing:
        raise ValueError("scenario suite must cover every group: " + ",".join(sorted(x.value for x in missing)))
    return suite


def _scenario(value: Any) -> EvaluationScenario:
    if not isinstance(value, dict) or not set(value).issubset(_SCENARIO_KEYS):
        raise ValueError("scenario contains unknown fields")
    required = {"id", "version", "group", "description", "inputs", "expected"}
    if not required.issubset(value):
        raise ValueError("scenario is missing required fields")
    expected = value["expected"]
    if not isinstance(expected, dict) or not set(expected).issubset(_EXPECTED_KEYS):
        raise ValueError("scenario expected schema is invalid")
    inputs = value["inputs"]
    if not isinstance(inputs, dict):
        raise ValueError("scenario inputs must be a mapping")
    rubric_raw = value.get("human_rubric", [])
    if not isinstance(rubric_raw, list):
        raise ValueError("human_rubric must be a list")
    rubric: list[HumanRubric] = []
    for item in rubric_raw:
        if not isinstance(item, dict) or not set(item).issubset(_RUBRIC_KEYS):
            raise ValueError("human rubric schema is invalid")
        rubric.append(HumanRubric(
            dimension=str(item.get("dimension") or ""),
            instruction=str(item.get("instruction") or ""),
            required=bool(item.get("required", True)),
        ))
    return EvaluationScenario(
        scenario_id=str(value["id"]), version=int(value["version"]),
        group=ScenarioGroup(str(value["group"])), description=str(value["description"]),
        inputs=inputs,
        expected=ExpectedOutcome(
            action=str(expected["action"]) if expected.get("action") is not None else None,
            state=str(expected["state"]) if expected.get("state") is not None else None,
            invariants=expected.get("invariants") or {},
        ),
        human_rubric=tuple(rubric),
    )

