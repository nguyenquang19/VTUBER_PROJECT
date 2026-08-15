"""Interfaces for versioned behavior evaluation and fine-tune gates (M8)."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from interfaces.base import Service

if TYPE_CHECKING:
    from services.evaluation.types import ObservedOutcome, ScenarioResult, ScenarioSuite


class EvaluationService(Service):
    @abstractmethod
    def suite(self) -> "ScenarioSuite":
        """Return the immutable versioned scenario suite."""

    @abstractmethod
    def evaluate(self, observed: "ObservedOutcome") -> "ScenarioResult":
        """Compare one sourced observed outcome against the scenario contract."""

    @abstractmethod
    def evaluate_many(
        self, observed: tuple["ObservedOutcome", ...],
    ) -> tuple["ScenarioResult", ...]:
        """Evaluate sourced outcomes without inventing missing observations."""


class EvaluationSimulationService(Service):
    @abstractmethod
    def simulate(self, *, seed: int | None = None) -> tuple[Any, ...]:
        """Run deterministic text-only scenarios with an injected clock."""


class EvaluationAcceptanceService(Service):
    @abstractmethod
    def run(self, *, seed: int | None = None) -> dict[str, Any]:
        """Run the bounded acceptance gate and return a sanitized report."""


class MoodABReviewService(Service):
    @abstractmethod
    def build(self, comparisons: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        """Build a deterministic blind Mood v1/v2 review sheet."""

    @abstractmethod
    def finalize(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Validate human review and calculate the cutover recommendation."""
