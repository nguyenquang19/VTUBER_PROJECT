"""Strict, feature-gated Director V2 conversational ownership selector."""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.director_v2 import (
    DIRECTOR_V2_TAKEOVER_ACTIONS,
    DIRECTOR_V2_TAKEOVER_STAGES,
    DirectorV2Proposal,
    DirectorV2TakeoverSelection,
    DirectorV2TakeoverService,
)


_CANONICAL_ACTIONS = frozenset({"WAIT", "READ_CHAT", "SELF_TALK", "FOLLOW_UP"})
_REQUIRED_ALIASES = {
    "ACK_DONATION": "READ_CHAT",
    "CONTINUE_THREAD": "FOLLOW_UP",
    "ASK_FOLLOW_UP": "FOLLOW_UP",
    "SHARE_GOAL_PROGRESS": "FOLLOW_UP",
}
_HARD_REASONS = frozenset({
    "emergency", "operator_hold", "safety_hold", "permission_hold",
    "transaction_conflict", "critical_state",
})


@dataclass(frozen=True)
class DirectorV2TakeoverConfig:
    stage: str
    max_recent_decisions: int
    max_reason_chars: int
    max_evidence_ids: int
    max_proposal_age_seconds: float
    stage_order: tuple[str, ...]
    stage_actions: Mapping[str, frozenset[str]]
    action_aliases: Mapping[str, str]

    def __post_init__(self) -> None:
        stage = _required_label(self.stage, "stage")
        if stage not in DIRECTOR_V2_TAKEOVER_STAGES:
            raise ValueError("director_v2_takeover stage is unsupported")
        for name in ("max_recent_decisions", "max_reason_chars", "max_evidence_ids"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        max_age = _finite_number(
            self.max_proposal_age_seconds, "max_proposal_age_seconds",
        )
        if max_age <= 0:
            raise ValueError("max_proposal_age_seconds must be positive")
        if not isinstance(self.stage_order, tuple):
            raise ValueError("stage_order must be a tuple")
        stage_order = tuple(_required_label(item, "stage_order") for item in self.stage_order)
        if stage_order != DIRECTOR_V2_TAKEOVER_STAGES:
            raise ValueError("stage_order must match the locked rollout order")
        if not isinstance(self.stage_actions, Mapping):
            raise ValueError("stage_actions must be a mapping")
        if set(self.stage_actions) != set(stage_order):
            raise ValueError("stage_actions must define every rollout stage exactly once")
        allowed = set(DIRECTOR_V2_TAKEOVER_ACTIONS)
        frozen_actions: dict[str, frozenset[str]] = {}
        previous: frozenset[str] = frozenset()
        for stage_name in stage_order:
            raw_actions = self.stage_actions[stage_name]
            if not isinstance(raw_actions, frozenset) or not raw_actions:
                raise ValueError("each stage action inventory must be a non-empty frozenset")
            actions = frozenset(
                _required_label(item, f"stage_actions.{stage_name}")
                for item in raw_actions
            )
            if not actions <= allowed:
                raise ValueError("stage action inventory contains an unsupported action")
            if not previous <= actions:
                raise ValueError("stage action inventory must expand monotonically")
            frozen_actions[stage_name] = actions
            previous = actions
        if frozen_actions["WAIT"] != frozenset({"WAIT"}):
            raise ValueError("WAIT stage must contain only WAIT")
        if frozen_actions[stage_order[-1]] != frozenset(allowed):
            raise ValueError("final rollout stage must contain every conversational action")
        if not isinstance(self.action_aliases, Mapping):
            raise ValueError("action_aliases must be a mapping")
        aliases = {
            _required_label(key, "action_aliases.key"):
            _required_label(value, "action_aliases.value")
            for key, value in self.action_aliases.items()
        }
        if aliases != _REQUIRED_ALIASES:
            raise ValueError("action_aliases must match the locked compatibility aliases")
        if not set(aliases) <= allowed or not set(aliases.values()) <= _CANONICAL_ACTIONS:
            raise ValueError("action_aliases contains an unsupported action")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "stage_order", stage_order)
        object.__setattr__(self, "max_proposal_age_seconds", max_age)
        object.__setattr__(self, "stage_actions", MappingProxyType(frozen_actions))
        object.__setattr__(self, "action_aliases", MappingProxyType(aliases))

    @classmethod
    def from_loader(cls, loader: Any) -> "DirectorV2TakeoverConfig":
        raw = loader.get("director", "director.director_v2_takeover", None)
        if not isinstance(raw, Mapping):
            raise ValueError("director_v2_takeover must be a mapping")
        raw_order = raw.get("stage_order")
        raw_stages = raw.get("stages")
        raw_aliases = raw.get("action_aliases")
        if not isinstance(raw_order, list):
            raise ValueError("director_v2_takeover.stage_order must be a list")
        if not isinstance(raw_stages, Mapping) or not isinstance(raw_aliases, Mapping):
            raise ValueError("director_v2_takeover stages and aliases must be mappings")
        stage_actions: dict[str, frozenset[str]] = {}
        for stage_name, actions in raw_stages.items():
            if not isinstance(stage_name, str) or not isinstance(actions, list):
                raise ValueError("director_v2_takeover stage entries must be string lists")
            if not all(isinstance(action, str) for action in actions):
                raise ValueError("director_v2_takeover actions must be strings")
            if len(actions) != len(set(actions)):
                raise ValueError("director_v2_takeover stage actions must be unique")
            stage_actions[stage_name] = frozenset(actions)
        return cls(
            stage=raw.get("stage"),
            max_recent_decisions=raw.get("max_recent_decisions"),
            max_reason_chars=raw.get("max_reason_chars"),
            max_evidence_ids=raw.get("max_evidence_ids"),
            max_proposal_age_seconds=raw.get("max_proposal_age_seconds"),
            stage_order=tuple(raw_order),
            stage_actions=stage_actions,
            action_aliases=dict(raw_aliases),
        )


class DirectorV2Takeover(DirectorV2TakeoverService):
    """Select ownership only; DirectorLoop remains the sole execution owner."""

    service_id = "director_v2_takeover"

    def __init__(
        self,
        config: DirectorV2TakeoverConfig,
        *,
        metrics: Any = None,
        enabled: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, DirectorV2TakeoverConfig):
            raise ValueError("config must be DirectorV2TakeoverConfig")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self._config = config
        self._metrics = metrics
        self._enabled = enabled
        self._clock = clock or time.time
        self._running = False
        self._records: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._sequence = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "DirectorV2Takeover":
        return cls(DirectorV2TakeoverConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled
        if not enabled:
            self._records.clear()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "director v2 takeover disabled")
        return HealthStatus.healthy(
            self.service_id, stage=self._config.stage, retained=len(self._records),
        )

    def evaluate(
        self,
        *,
        legacy_action: str,
        proposal: DirectorV2Proposal | None,
        evidence_ids: tuple[str, ...] = (),
    ) -> DirectorV2TakeoverSelection:
        action = _required_action(legacy_action)
        if not self._enabled:
            return self._selection(False, "feature_disabled", action, proposal, record=False)
        try:
            evidence = _strict_evidence(evidence_ids, self._config.max_evidence_ids)
        except ValueError:
            return self._selection(False, "evidence_invalid", action, proposal)
        if action not in self._config.stage_actions[self._config.stage]:
            return self._selection(False, "stage_blocked", action, proposal)
        if proposal is None:
            return self._selection(False, "proposal_missing", action, proposal)
        if not isinstance(proposal, DirectorV2Proposal):
            return self._selection(False, "proposal_malformed", action, None)
        try:
            now = _finite_number(self._clock(), "clock")
        except (TypeError, ValueError):
            return self._selection(False, "clock_invalid", action, proposal)
        if proposal.created_at > now:
            return self._selection(False, "proposal_from_future", action, proposal)
        if now - proposal.created_at > self._config.max_proposal_age_seconds:
            return self._selection(False, "proposal_stale", action, proposal)
        try:
            proposal_action = _required_action(proposal.action_type)
        except ValueError:
            return self._selection(False, "proposal_action_invalid", action, proposal)
        normalized_action = self._config.action_aliases.get(action, action)
        normalized_proposal = self._config.action_aliases.get(proposal_action, proposal_action)
        if normalized_proposal != normalized_action:
            return self._selection(False, "action_mismatch", action, proposal)
        reason_codes = set(proposal.reason_codes)
        if any(code.startswith("capability_") for code in reason_codes):
            return self._selection(False, "capability_rejected", action, proposal)
        if reason_codes & _HARD_REASONS:
            return self._selection(False, "hard_hold", action, proposal)
        if any(
            code == "feature_disabled"
            or (code.startswith("source_") and code.endswith("_failed"))
            or code.endswith("_rejected")
            or code.startswith("candidate_")
            for code in reason_codes
        ):
            return self._selection(False, "proposal_rejected", action, proposal)
        if normalized_action == "WAIT" and (
            proposal.candidate_id != "wait" or proposal.capability_id != "WAIT"
        ):
            return self._selection(False, "wait_evidence_invalid", action, proposal)
        if normalized_action == "READ_CHAT" and proposal.candidate_id not in evidence:
            return self._selection(False, "chat_evidence_missing", action, proposal)
        if normalized_action == "FOLLOW_UP" and proposal.candidate_id not in evidence:
            return self._selection(False, "thread_goal_evidence_missing", action, proposal)
        return self._selection(True, "accepted", action, proposal)

    def snapshot(self) -> dict[str, object]:
        recent = list(self._records.values())[-self._config.max_recent_decisions:]
        return {
            "enabled": self._enabled,
            "stage": self._config.stage,
            "counts": dict(sorted(self._counts.items())),
            "current": recent[-1] if recent else None,
            "recent": list(reversed(recent)),
        }

    def get_metrics(self) -> dict[str, Any]:
        return {
            "director_v2_takeover_enabled": self._enabled,
            "director_v2_takeover_stage": self._config.stage,
            "director_v2_takeover_retained": len(self._records),
            "director_v2_takeover_outcomes": dict(sorted(self._counts.items())),
        }

    def _selection(
        self,
        accepted: bool,
        reason: str,
        action: str,
        proposal: DirectorV2Proposal | None,
        *,
        record: bool = True,
    ) -> DirectorV2TakeoverSelection:
        proposal_id = proposal.proposal_id if isinstance(proposal, DirectorV2Proposal) else None
        reason = reason[:self._config.max_reason_chars]
        selection = DirectorV2TakeoverSelection(
            accepted=accepted,
            stage=self._config.stage,
            reason_code=reason,
            action_type=action,
            proposal_id=proposal_id,
            decision_owner="director_v2" if accepted else "legacy",
        )
        if not record:
            return selection
        self._sequence += 1
        self._records[self._sequence] = {
            "accepted": accepted,
            "decision_owner": selection.decision_owner,
            "stage": self._config.stage,
            "reason_code": reason,
            "action_type": action,
            "proposal_id": proposal_id,
        }
        while len(self._records) > self._config.max_recent_decisions:
            self._records.popitem(last=False)
        key = f"{self._config.stage}:{reason}"
        self._counts[key] = self._counts.get(key, 0) + 1
        try:
            recorder = getattr(self._metrics, "record_director_v2_takeover", None)
            if callable(recorder):
                recorder(self._config.stage, reason, len(self._records))
        except Exception:
            pass
        return selection


def _required_label(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_action(value: object) -> str:
    action = _required_label(value, "action").upper()
    if action not in DIRECTOR_V2_TAKEOVER_ACTIONS:
        raise ValueError("action is unsupported")
    return action


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def _strict_evidence(value: object, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("evidence_ids must be a tuple")
    if len(value) > maximum:
        raise ValueError("evidence_ids exceeds configured capacity")
    result = tuple(_required_label(item, "evidence_ids") for item in value)
    if len(set(result)) != len(result):
        raise ValueError("evidence_ids must be unique")
    return result
