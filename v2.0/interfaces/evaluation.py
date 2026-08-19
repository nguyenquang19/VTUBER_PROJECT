"""Interfaces for versioned behavior evaluation and fine-tune gates (M8)."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Mapping

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


class HumanLikeCalibrationService(Service):
    @abstractmethod
    def build(self, comparisons: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        """Build a sanitized blind review artifact without hidden internals."""

    @abstractmethod
    def finalize(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Validate human scores and produce non-automatic calibration evidence."""

    @abstractmethod
    def reveal_internals(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Reveal bounded structured metadata only after finalized human scoring."""


class TrajectoryRecorderService(Service):
    @abstractmethod
    def record_decision(self, **kwargs: Any) -> str | None:
        """Record bounded structured decision evidence without raw prompt/memory."""

    @abstractmethod
    def update_result(self, trajectory_id: str, **kwargs: Any) -> bool:
        """Attach action/verification and next snapshot IDs to a trajectory record."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded replay-safe trajectory records."""

class ProductReleaseGateService(Service):
    @abstractmethod
    def evaluate(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Fail closed on release evidence and never authorize deployment mutation."""
