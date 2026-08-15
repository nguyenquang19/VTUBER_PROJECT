from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.evaluation.harness import ScenarioEvaluationHarness
from services.evaluation.simulator import TextScenarioSimulator


ROOT = Path(__file__).resolve().parents[2]


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


def _simulator() -> TextScenarioSimulator:
    loader = _loader()
    harness = ScenarioEvaluationHarness.from_loader(loader)
    return TextScenarioSimulator.from_loader(loader, harness.suite())


def test_same_seed_and_injected_clock_replay_exactly() -> None:
    simulator = _simulator()
    first = simulator.simulate(seed=17)
    second = simulator.simulate(seed=17)
    assert len(first) == 7
    assert tuple(item.replay_key() for item in first) == tuple(
        item.replay_key() for item in second
    )
    assert {item.started_at for item in first} == {1000.0}
    assert all(len(item.trace) <= simulator.max_steps for item in first)


def test_seed_controls_scenario_order_without_changing_contract() -> None:
    first = _simulator().simulate(seed=1)
    second = _simulator().simulate(seed=2)
    assert [item.scenario_id for item in first] != [item.scenario_id for item in second]
    assert {item.scenario_id for item in first} == {item.scenario_id for item in second}


def test_fault_allow_list_is_fail_fast() -> None:
    loader = _loader()
    harness = ScenarioEvaluationHarness.from_loader(loader)
    simulator = TextScenarioSimulator(
        harness.suite(), default_seed=1, clock_start=0, clock_tick_s=0.1,
        max_steps=64, allowed_faults=("none",),
    )
    with pytest.raises(ValueError, match="not allowed"):
        simulator.simulate()


def test_disabled_simulator_has_no_observations() -> None:
    simulator = _simulator()
    simulator.enabled = False
    assert simulator.simulate() == ()
