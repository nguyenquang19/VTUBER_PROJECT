"""Strict human-like calibration contracts for blind Phase 14 review."""
from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Any, Mapping

from interfaces.base import Service


class HumanLikeCalibrationService(Service):
    """Build blind artifacts and reveal sealed metadata only after persisted review."""

    @abstractmethod
    def build(
        self, comparisons: tuple[Mapping[str, Any], ...],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a reviewer artifact and a separate sealed manifest."""

    @abstractmethod
    async def finalize(
        self, review_path: Path, sealed_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read a persisted score artifact, validate it, then reveal bounded metadata."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return bounded counters without review text or sealed internals."""
