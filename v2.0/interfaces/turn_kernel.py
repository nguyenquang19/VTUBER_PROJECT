"""Strict contracts for the single live turn-scheduling boundary."""
from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from interfaces.base import Service
from interfaces.cognition import (
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveOpportunityKind,
)


class TurnRolloutMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    PRIMARY = "PRIMARY"
    RELEASED = "RELEASED"


class TurnOwner(str, Enum):
    COMPATIBILITY = "COMPATIBILITY"
    BRAIN = "BRAIN"


KERNEL_HARD_REASON_CODES = frozenset({
    "emergency",
    "operator_hold",
    "safety_hold",
    "permission_hold",
    "transaction_conflict",
    "critical_state",
    "source_failure",
})


@dataclass(frozen=True)
class KernelConfig:
    schema_version: int
    rollout_mode: TurnRolloutMode
    tick_seconds: float
    max_recent_selections: int
    max_reason_codes: int
    max_id_chars: int
    reason_codes: tuple[str, ...]

    _KEYS = frozenset({
        "schema_version", "rollout_mode", "tick_seconds",
        "max_recent_selections", "max_reason_codes", "max_id_chars",
        "reason_codes",
    })

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if not isinstance(self.rollout_mode, TurnRolloutMode):
            raise ValueError("rollout_mode must be a TurnRolloutMode")
        if self.rollout_mode not in (TurnRolloutMode.OFF, TurnRolloutMode.SHADOW):
            raise ValueError("S4 runtime allows only OFF or SHADOW")
        if (
            isinstance(self.tick_seconds, bool)
            or not isinstance(self.tick_seconds, float)
            or not math.isfinite(self.tick_seconds)
            or self.tick_seconds <= 0
        ):
            raise ValueError("tick_seconds must be a finite positive float")
        for name in ("max_recent_selections", "max_reason_codes", "max_id_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.reason_codes, tuple):
            raise ValueError("reason_codes must be a tuple")
        if not self.reason_codes or len(self.reason_codes) > self.max_reason_codes:
            raise ValueError("reason_codes must be non-empty and bounded")
        normalized: list[str] = []
        for value in self.reason_codes:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("reason_codes must contain non-empty strings")
            item = value.strip()
            if len(item) > self.max_id_chars:
                raise ValueError("reason code exceeds max_id_chars")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise ValueError("reason_codes must be unique")
        if set(normalized) != KERNEL_HARD_REASON_CODES:
            raise ValueError("reason_codes must match the S4 hard-preflight allowlist")
        object.__setattr__(self, "reason_codes", tuple(normalized))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "KernelConfig":
        if not isinstance(value, Mapping):
            raise ValueError("kernel config must be a mapping")
        keys = set(value)
        if keys != cls._KEYS:
            raise ValueError(
                "kernel config keys mismatch: "
                f"missing={sorted(cls._KEYS - keys)}, unknown={sorted(keys - cls._KEYS)}"
            )
        raw_reasons = value["reason_codes"]
        if not isinstance(raw_reasons, list):
            raise ValueError("reason_codes must be a YAML list")
        try:
            mode = TurnRolloutMode(str(value["rollout_mode"]).upper())
        except (TypeError, ValueError) as exc:
            raise ValueError("rollout_mode is unsupported") from exc
        payload = dict(value)
        payload["rollout_mode"] = mode
        payload["reason_codes"] = tuple(raw_reasons)
        return cls(**payload)


@dataclass(frozen=True)
class TurnOpportunity:
    schema_version: int
    opportunity_id: str
    opened_at: datetime
    kind: CognitiveOpportunityKind
    material_change_ref: str
    context_request: CognitiveContextRequest

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.opportunity_id, "opportunity_id")
        opened = _utc(self.opened_at, "opened_at")
        object.__setattr__(self, "opened_at", opened)
        if not isinstance(self.kind, CognitiveOpportunityKind):
            raise ValueError("kind must be a CognitiveOpportunityKind")
        _identifier(self.material_change_ref, "material_change_ref")
        if not isinstance(self.context_request, CognitiveContextRequest):
            raise ValueError("context_request must be a CognitiveContextRequest")
        if self.context_request.requested_at != opened:
            raise ValueError("opportunity and context timestamps must match")


@dataclass(frozen=True)
class TurnPreflight:
    schema_version: int
    opportunity_id: str
    checked_at: datetime
    allowed: bool
    hard_state: CognitiveHardState
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.opportunity_id, "opportunity_id")
        object.__setattr__(self, "checked_at", _utc(self.checked_at, "checked_at"))
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a bool")
        if not isinstance(self.hard_state, CognitiveHardState):
            raise ValueError("hard_state must be a CognitiveHardState")
        reasons = _reason_tuple(self.reason_codes)
        if self.allowed == bool(reasons):
            raise ValueError("allowed requires no reasons; rejected requires reasons")
        object.__setattr__(self, "reason_codes", reasons)


@dataclass(frozen=True)
class TurnOwnerSelection:
    schema_version: int
    opportunity_id: str
    selected_at: datetime
    rollout_mode: TurnRolloutMode
    owner: TurnOwner
    selection_ref: str

    def __post_init__(self) -> None:
        _schema(self.schema_version)
        _identifier(self.opportunity_id, "opportunity_id")
        object.__setattr__(self, "selected_at", _utc(self.selected_at, "selected_at"))
        if not isinstance(self.rollout_mode, TurnRolloutMode):
            raise ValueError("rollout_mode must be a TurnRolloutMode")
        if not isinstance(self.owner, TurnOwner):
            raise ValueError("owner must be a TurnOwner")
        if (
            self.rollout_mode in (TurnRolloutMode.OFF, TurnRolloutMode.SHADOW)
            and self.owner is not TurnOwner.COMPATIBILITY
        ):
            raise ValueError("OFF and SHADOW require COMPATIBILITY owner")
        _identifier(self.selection_ref, "selection_ref")


class TurnKernelService(Service):
    @abstractmethod
    async def tick_once(self) -> object:
        """Run exactly one selected compatibility turn in S4."""

    @abstractmethod
    def notify_input_activity(self) -> None:
        """Preempt subordinate shadow work for live input."""

    @abstractmethod
    def recent_selections(self, limit: int | None = None) -> tuple[TurnOwnerSelection, ...]:
        """Return bounded immutable selections oldest to newest."""


def _schema(value: Any) -> None:
    if isinstance(value, bool) or value != 1:
        raise ValueError("schema_version must be 1")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a bounded non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must be trimmed")
    return value


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _reason_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("reason_codes must be a tuple")
    for item in value:
        _identifier(item, "reason_code")
    if len(set(value)) != len(value):
        raise ValueError("reason_codes must be unique")
    return value
