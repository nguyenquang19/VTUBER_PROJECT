"""Strict contracts for the single live turn-scheduling boundary."""
from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from interfaces.base import Service
from interfaces.cognition import (
    CognitionConfig,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
    CognitiveOpportunityKind,
)


class TurnRolloutMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    PUBLIC_BRAIN = "PUBLIC_BRAIN"
    CANARY = "CANARY"
    PRIMARY = "PRIMARY"
    RELEASED = "RELEASED"


class TurnOwner(str, Enum):
    COMPATIBILITY = "COMPATIBILITY"
    BRAIN = "BRAIN"


class TurnRouteOutcome(str, Enum):
    COMPATIBILITY = "COMPATIBILITY"
    BRAIN_SPEAK = "BRAIN_SPEAK"
    BRAIN_WAIT = "BRAIN_WAIT"
    FALLBACK = "FALLBACK"


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
    brain_canary_roles: tuple[str, ...]

    _KEYS = frozenset({
        "schema_version", "rollout_mode", "tick_seconds",
        "max_recent_selections", "max_reason_codes", "max_id_chars",
        "reason_codes", "brain_canary_roles",
    })

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if not isinstance(self.rollout_mode, TurnRolloutMode):
            raise ValueError("rollout_mode must be a TurnRolloutMode")
        if self.rollout_mode not in (
            TurnRolloutMode.OFF,
            TurnRolloutMode.SHADOW,
            TurnRolloutMode.PUBLIC_BRAIN,
        ):
            raise ValueError("runtime allows only OFF, SHADOW, or PUBLIC_BRAIN")
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
        roles = _reason_tuple(self.brain_canary_roles)
        if not roles or any(role not in {"owner", "moderator"} for role in roles):
            raise ValueError("brain_canary_roles must use owner/moderator roles")
        object.__setattr__(self, "brain_canary_roles", roles)

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
        raw_roles = value["brain_canary_roles"]
        if not isinstance(raw_reasons, list) or not isinstance(raw_roles, list):
            raise ValueError("reason_codes and brain_canary_roles must be YAML lists")
        try:
            raw_mode = str(value["rollout_mode"]).upper()
            mode = TurnRolloutMode.PUBLIC_BRAIN if raw_mode == "BRAIN" else TurnRolloutMode(raw_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("rollout_mode is unsupported") from exc
        payload = dict(value)
        payload["rollout_mode"] = mode
        payload["reason_codes"] = tuple(raw_reasons)
        payload["brain_canary_roles"] = tuple(raw_roles)
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


@dataclass(frozen=True)
class PublicTurnRoute:
    """Typed route projection; never carries raw context or viewer identity."""

    config: InitVar[KernelConfig]
    cognition_config: InitVar[CognitionConfig]
    schema_version: int
    opportunity_id: str
    owner: TurnOwner
    outcome: TurnRouteOutcome
    mode: CognitiveMode
    speech_text: str | None
    source_turn_id: str | None
    evidence_refs: tuple[str, ...]
    reason_code: str

    def __post_init__(
        self, config: KernelConfig, cognition_config: CognitionConfig,
    ) -> None:
        _schema(self.schema_version)
        _identifier(self.opportunity_id, "opportunity_id")
        if not isinstance(self.owner, TurnOwner):
            raise ValueError("owner must be a TurnOwner")
        if not isinstance(self.outcome, TurnRouteOutcome):
            raise ValueError("outcome must be a TurnRouteOutcome")
        if not isinstance(self.mode, CognitiveMode):
            raise ValueError("mode must be a CognitiveMode")
        if self.mode is CognitiveMode.PROPOSE_ACTION:
            raise ValueError("B3 public route cannot expose PROPOSE_ACTION")
        if self.speech_text is not None:
            if (
                not isinstance(self.speech_text, str)
                or self.speech_text != self.speech_text.strip()
                or not self.speech_text
                or len(self.speech_text) > cognition_config.max_speech_chars
            ):
                raise ValueError("speech_text must be bounded trimmed text")
        if self.source_turn_id is not None:
            _identifier(self.source_turn_id, "source_turn_id")
        refs = _reason_tuple(self.evidence_refs)
        if len(refs) > cognition_config.max_evidence_refs:
            raise ValueError("evidence_refs exceeds cognition bound")
        object.__setattr__(self, "evidence_refs", refs)
        reason = _identifier(self.reason_code, "reason_code")
        if len(reason) > config.max_id_chars:
            raise ValueError("reason_code exceeds kernel bound")
        if self.owner is TurnOwner.BRAIN:
            expected_outcome = (
                TurnRouteOutcome.BRAIN_SPEAK
                if self.mode is CognitiveMode.SPEAK
                else TurnRouteOutcome.BRAIN_WAIT
            )
            if self.outcome is not expected_outcome:
                raise ValueError("Brain route outcome must match its effective mode")
            if (self.mode is CognitiveMode.SPEAK) != (self.speech_text is not None):
                raise ValueError("Brain SPEAK requires text and WAIT forbids text")
            if self.source_turn_id is None:
                raise ValueError("Brain route requires source_turn_id provenance")
            if self.mode is CognitiveMode.SPEAK and not refs:
                raise ValueError("Brain SPEAK requires grounded evidence_refs")
            if self.mode is CognitiveMode.WAIT and refs:
                raise ValueError("Brain WAIT cannot expose evidence_refs")
        else:
            if self.outcome not in {
                TurnRouteOutcome.COMPATIBILITY, TurnRouteOutcome.FALLBACK,
            }:
                raise ValueError("compatibility owner requires compatibility outcome")
            if self.speech_text is not None or self.source_turn_id is not None or refs:
                raise ValueError("compatibility route cannot carry Brain output")


class TurnKernelService(Service):
    @abstractmethod
    async def tick_once(self) -> object:
        """Run exactly one selected public turn."""

    @abstractmethod
    async def route_decision(
        self, decision: Any, director_input: Any, decision_id: str | None,
    ) -> PublicTurnRoute | None:
        """Route one compatibility decision through hard preflight and rollout policy."""

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
