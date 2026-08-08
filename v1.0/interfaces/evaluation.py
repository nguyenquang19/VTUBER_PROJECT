"""Interfaces for versioned behavior evaluation and fine-tune gates (M8)."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

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

