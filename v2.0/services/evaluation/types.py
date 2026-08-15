"""Immutable value objects for M8 evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ScenarioGroup(str, Enum):
    DIRECTOR = "director"
    AGENCY = "agency"
    CONTINUITY = "continuity"
    SAFETY = "safety"
    ENVIRONMENT = "environment"
    PERSONA = "persona"


class EvalOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_OBSERVED = "not_observed"


class EvaluationFault(str, Enum):
    NONE = "none"
    GENERATION_ERROR = "generation_error"
    FILTER_REJECT = "filter_reject"
    DELIVERY_ERROR = "delivery_error"
    DUPLICATE_EVENT = "duplicate_event"
    LOGGING_ERROR = "logging_error"
    DASHBOARD_ERROR = "dashboard_error"
    SHUTDOWN_BEFORE_COMMIT = "shutdown_before_commit"


@dataclass(frozen=True)
class ExpectedOutcome:
    action: str | None = None
    state: str | None = None
    invariants: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action is None and self.state is None and not self.invariants:
            raise ValueError("expected outcome needs action, state, or invariant")
        object.__setattr__(self, "invariants", MappingProxyType(dict(self.invariants)))


@dataclass(frozen=True)
class HumanRubric:
    dimension: str
    instruction: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.dimension.strip() or not self.instruction.strip():
            raise ValueError("human rubric requires dimension and instruction")


@dataclass(frozen=True)
class EvaluationScenario:
    scenario_id: str
    version: int
    group: ScenarioGroup
    description: str
    inputs: Mapping[str, Any]
    expected: ExpectedOutcome
    human_rubric: tuple[HumanRubric, ...] = ()

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or self.version < 1 or not self.description.strip():
            raise ValueError("scenario requires id, positive version, and description")
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "human_rubric", tuple(self.human_rubric))


@dataclass(frozen=True)
class ScenarioSuite:
    schema_version: int
    contract_id: str
    scenarios: tuple[EvaluationScenario, ...]

    def __post_init__(self) -> None:
        if self.schema_version < 1 or not self.contract_id.strip() or not self.scenarios:
            raise ValueError("scenario suite is incomplete")
        ids = [item.scenario_id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario ids must be unique")
        object.__setattr__(self, "scenarios", tuple(self.scenarios))


@dataclass(frozen=True)
class ObservedOutcome:
    scenario_id: str
    action: str | None = None
    state: str | None = None
    invariants: Mapping[str, Any] = field(default_factory=dict)
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("observed outcome requires scenario id")
        object.__setattr__(self, "invariants", MappingProxyType(dict(self.invariants)))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    group: ScenarioGroup
    outcome: EvalOutcome
    checks: Mapping[str, bool]
    source_refs: tuple[str, ...]
    human_review_required: bool
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "group": self.group.value,
            "outcome": self.outcome.value,
            "checks": dict(self.checks),
            "source_refs": list(self.source_refs),
            "human_review_required": self.human_review_required,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SimulationResult:
    scenario_id: str
    seed: int
    fault: EvaluationFault
    started_at: float
    trace: tuple[str, ...]
    observed: ObservedOutcome

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace", tuple(self.trace))

    def replay_key(self) -> tuple[Any, ...]:
        return (
            self.scenario_id,
            self.seed,
            self.fault.value,
            self.started_at,
            self.trace,
            self.observed.action,
            self.observed.state,
            tuple(sorted(self.observed.invariants.items())),
            self.observed.source_refs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "fault": self.fault.value,
            "started_at": self.started_at,
            "trace": list(self.trace),
            "observed": {
                "action": self.observed.action,
                "state": self.observed.state,
                "invariants": dict(self.observed.invariants),
                "source_refs": list(self.observed.source_refs),
            },
        }
