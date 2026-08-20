"""Strict Phase 15 release configuration and source identity helpers."""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_CONFIG_KEYS = {
    "schema_version", "target_version", "artifact_max_age_s",
    "max_future_skew_s", "max_artifacts", "max_label_chars",
    "required_test_groups", "required_preflight_checks",
    "correctness_zero_counters", "human_quality",
}
_HUMAN_KEYS = {
    "min_pairs", "minimum_previous_build_delta",
    "max_ai_smell_rate_increase", "minimum_character_delta",
}
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ReleaseReadinessConfig:
    schema_version: int
    target_version: str
    artifact_max_age_s: float
    max_future_skew_s: float
    max_artifacts: int
    max_label_chars: int
    required_test_groups: tuple[str, ...]
    required_preflight_checks: tuple[str, ...]
    correctness_zero_counters: tuple[str, ...]
    human_min_pairs: int
    minimum_previous_build_delta: float
    max_ai_smell_rate_increase: float
    minimum_character_delta: float

    @classmethod
    def from_loader(cls, loader: Any) -> "ReleaseReadinessConfig":
        raw = loader.get("operations", "release_readiness", None)
        if not isinstance(raw, Mapping) or set(raw) != _CONFIG_KEYS:
            raise ValueError("release_readiness keys are invalid")
        human = raw.get("human_quality")
        if not isinstance(human, Mapping) or set(human) != _HUMAN_KEYS:
            raise ValueError("release_readiness.human_quality keys are invalid")
        config = cls(
            schema_version=_positive_int(raw["schema_version"], "schema_version"),
            target_version=_semver(raw["target_version"], "target_version"),
            artifact_max_age_s=_finite(
                raw["artifact_max_age_s"], "artifact_max_age_s", positive=True,
            ),
            max_future_skew_s=_finite(
                raw["max_future_skew_s"], "max_future_skew_s", non_negative=True,
            ),
            max_artifacts=_positive_int(raw["max_artifacts"], "max_artifacts"),
            max_label_chars=_positive_int(raw["max_label_chars"], "max_label_chars"),
            required_test_groups=_strict_unique_strings(
                raw["required_test_groups"], "required_test_groups",
            ),
            required_preflight_checks=_strict_unique_strings(
                raw["required_preflight_checks"], "required_preflight_checks",
            ),
            correctness_zero_counters=_strict_unique_strings(
                raw["correctness_zero_counters"], "correctness_zero_counters",
            ),
            human_min_pairs=_positive_int(human["min_pairs"], "human.min_pairs"),
            minimum_previous_build_delta=_finite(
                human["minimum_previous_build_delta"],
                "human.minimum_previous_build_delta", positive=True,
            ),
            max_ai_smell_rate_increase=_finite(
                human["max_ai_smell_rate_increase"],
                "human.max_ai_smell_rate_increase", non_negative=True,
            ),
            minimum_character_delta=_finite(
                human["minimum_character_delta"],
                "human.minimum_character_delta", non_negative=True,
            ),
        )
        if config.schema_version != 2:
            raise ValueError("release_readiness schema_version must be 2")
        if config.max_artifacts < 5:
            raise ValueError("release_readiness max_artifacts must cover all five gates")
        return config


@dataclass(frozen=True)
class SourceState:
    revision: str
    clean: bool

    def __post_init__(self) -> None:
        if not _SHA.fullmatch(self.revision):
            raise ValueError("source revision must be a lowercase full Git SHA")
        if type(self.clean) is not bool:
            raise ValueError("source clean state must be a bool")


def inspect_source_state(repo_root: Path) -> SourceState:
    root = repo_root.resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return SourceState(revision=revision, clean=not bool(status.strip()))


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite(
    value: Any, name: str, *, positive: bool = False, non_negative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if non_negative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _strict_unique_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ValueError(f"{name} must contain trimmed non-empty strings")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must be unique")
    return tuple(result)


def _semver(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        raise ValueError(f"{name} must be strict semantic version")
    return value
