"""Config-driven mood and tone-flag influence on goals and Director actions (M5.1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.animation import MoodState
from interfaces.state import GoalKind


@dataclass(frozen=True)
class MoodPolicyConfig:
    activation_floor: int
    priority_min: int
    priority_max: int
    proactive_score_floor: float
    agenda_deltas: dict[str, dict[str, int]]
    director_scores: dict[str, dict[str, float]]
    tone_flag_deltas: dict[str, dict[str, dict[str, float]]]

    @classmethod
    def from_loader(cls, loader: Any) -> "MoodPolicyConfig":
        raw = loader.get("hosting", "mood_policy", {}) or {}
        value = cls(
            activation_floor=int(raw.get("activation_floor", 6)),
            priority_min=int(raw.get("priority_min", 0)),
            priority_max=int(raw.get("priority_max", 100)),
            proactive_score_floor=float(raw.get("proactive_score_floor", 50)),
            agenda_deltas=_nested_ints(raw.get("agenda_deltas", {})),
            director_scores=_nested_floats(raw.get("director_scores", {})),
            tone_flag_deltas=_flag_deltas(raw.get("tone_flag_deltas", {})),
        )
        if not 0 <= value.activation_floor <= 10:
            raise ValueError("mood activation floor must be within [0, 10]")
        if value.priority_min > value.priority_max:
            raise ValueError("mood priority bounds are invalid")
        return value


class MoodActionPolicy:
    """Pure score adjustment; safety/donation hard priorities remain outside this policy."""

    def __init__(
        self, config: MoodPolicyConfig, *, metrics: Any = None, enabled: bool = True,
    ) -> None:
        self.config = config
        self._metrics = metrics
        self.enabled = bool(enabled)

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool | None = None,
    ) -> "MoodActionPolicy":
        raw_enabled = bool(loader.get("hosting", "mood_policy.enabled", True))
        return cls(
            MoodPolicyConfig.from_loader(loader), metrics=metrics,
            enabled=raw_enabled if enabled is None else enabled,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def goal_priority(
        self,
        kind: GoalKind,
        base_priority: int,
        mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> int:
        # M10.6 correctness boundary: mood/tone can shape delivery, but never
        # reorder grounded goals or hard priorities.
        return int(base_priority)

    def action_score(
        self,
        action: str,
        base_score: float,
        mood: MoodState | None,
        tone_flags: set[str] | tuple[str, ...] = (),
    ) -> float:
        delta = self._mood_delta("director", action, mood)
        delta += self._flag_delta("director", action, tone_flags)
        if self.enabled and delta:
            self._record("director", f"{action}:{delta:+g}")
        return float(base_score) + delta

    def proactive_ready(
        self, mood: MoodState | None, tone_flags: set[str] | tuple[str, ...] = (),
    ) -> bool:
        if not self.enabled:
            return False
        score = self.action_score("self_talk", 30.0, mood, tone_flags)
        return score >= self.config.proactive_score_floor

    def _mood_delta(self, target: str, key: str, mood: MoodState | None) -> float:
        if not self.enabled or mood is None:
            return 0.0
        dominant = mood.dominant()
        if dominant == "neutral" or int(getattr(mood, dominant, 0)) < self.config.activation_floor:
            return 0.0
        table = (
            self.config.agenda_deltas if target == "agenda" else self.config.director_scores
        )
        return float(table.get(dominant, {}).get(key, 0.0))

    def _flag_delta(
        self, target: str, key: str, flags: set[str] | tuple[str, ...],
    ) -> float:
        if not self.enabled:
            return 0.0
        return sum(
            float(self.config.tone_flag_deltas.get(flag, {}).get(target, {}).get(key, 0.0))
            for flag in set(flags)
        )

    def _record(self, target: str, reason: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_mood_adjustment"):
            try:
                self._metrics.record_mood_adjustment(target, reason)
            except Exception:
                pass


def _nested_ints(value: Any) -> dict[str, dict[str, int]]:
    return {
        str(outer): {str(key): int(item) for key, item in dict(inner or {}).items()}
        for outer, inner in dict(value or {}).items()
    }


def _nested_floats(value: Any) -> dict[str, dict[str, float]]:
    return {
        str(outer): {str(key): float(item) for key, item in dict(inner or {}).items()}
        for outer, inner in dict(value or {}).items()
    }


def _flag_deltas(value: Any) -> dict[str, dict[str, dict[str, float]]]:
    return {
        str(flag): {
            str(target): {str(key): float(item) for key, item in dict(entries or {}).items()}
            for target, entries in dict(targets or {}).items()
        }
        for flag, targets in dict(value or {}).items()
    }
