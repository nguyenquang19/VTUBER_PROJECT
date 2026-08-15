"""Feature-gated, agreement-only Director V2 conversational takeover."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.director_v2 import (
    DirectorV2Proposal, DirectorV2TakeoverSelection, DirectorV2TakeoverService,
)


@dataclass(frozen=True)
class DirectorV2TakeoverConfig:
    stage: str
    max_recent_decisions: int
    max_reason_chars: int
    stage_order: tuple[str, ...]
    stage_actions: Mapping[str, frozenset[str]]
    action_aliases: Mapping[str, str]

    @classmethod
    def from_loader(cls, loader: Any) -> "DirectorV2TakeoverConfig":
        raw = loader.get("director", "director.director_v2_takeover", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("director_v2_takeover must be a mapping")
        stage_order = tuple(_clean(value) for value in raw.get("stage_order", ()) if _clean(value))
        stages = raw.get("stages", {})
        aliases = raw.get("action_aliases", {})
        if not isinstance(stages, Mapping) or not isinstance(aliases, Mapping):
            raise ValueError("director_v2_takeover stages and aliases must be mappings")
        stage_actions = {
            _clean(stage): frozenset(_clean(action) for action in actions if _clean(action))
            for stage, actions in stages.items() if isinstance(actions, (list, tuple))
        }
        config = cls(
            stage=_clean(raw.get("stage")),
            max_recent_decisions=int(raw.get("max_recent_decisions", 0)),
            max_reason_chars=int(raw.get("max_reason_chars", 0)),
            stage_order=stage_order,
            stage_actions=stage_actions,
            action_aliases={_clean(key): _clean(value) for key, value in aliases.items()},
        )
        if not config.stage or config.max_recent_decisions <= 0 or config.max_reason_chars <= 0:
            raise ValueError("director_v2_takeover bounds and stage must be positive/non-empty")
        if not config.stage_order or config.stage not in config.stage_order:
            raise ValueError("director_v2_takeover stage must be in stage_order")
        if set(config.stage_order) != set(config.stage_actions):
            raise ValueError("director_v2_takeover stages must match stage_order")
        if any(not actions for actions in config.stage_actions.values()):
            raise ValueError("director_v2_takeover stage action lists must be non-empty")
        return config


class DirectorV2Takeover(DirectorV2TakeoverService):
    """Accept only an evidence-backed V2/legacy agreement; never executes a turn."""

    service_id = "director_v2_takeover"

    def __init__(
        self, config: DirectorV2TakeoverConfig, *, metrics: Any = None, enabled: bool = False,
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._running = False
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._sequence = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "DirectorV2Takeover":
        return cls(DirectorV2TakeoverConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "director v2 takeover disabled")
        return HealthStatus.healthy(self.service_id, stage=self._config.stage, retained=len(self._records))

    def evaluate(
        self, *, legacy_action: str, proposal: DirectorV2Proposal | None,
        evidence_ids: tuple[str, ...] = (),
    ) -> DirectorV2TakeoverSelection:
        action = _clean(legacy_action)
        if not self._enabled:
            return self._record(False, "feature_disabled", action, proposal)
        if action not in self._config.stage_actions[self._config.stage]:
            return self._record(False, "stage_blocked", action, proposal)
        if proposal is None:
            return self._record(False, "proposal_missing", action, proposal)
        normalized = self._config.action_aliases.get(action, action)
        if _clean(proposal.action_type) != normalized:
            return self._record(False, "action_mismatch", action, proposal)
        reason_codes = set(proposal.reason_codes)
        if any(code.startswith("capability_") for code in reason_codes):
            return self._record(False, "capability_rejected", action, proposal)
        if reason_codes & {"emergency", "operator_hold", "safety_hold", "permission_hold", "transaction_conflict", "critical_state"}:
            return self._record(False, "hard_hold", action, proposal)
        if action in {"READ_CHAT", "ACK_DONATION"} and proposal.candidate_id not in set(evidence_ids):
            return self._record(False, "chat_evidence_missing", action, proposal)
        if action in {"FOLLOW_UP", "CONTINUE_THREAD", "ASK_FOLLOW_UP", "SHARE_GOAL_PROGRESS"} and proposal.candidate_id not in set(evidence_ids):
            return self._record(False, "thread_goal_evidence_missing", action, proposal)
        return self._record(True, "accepted", action, proposal)

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

    def _record(
        self, accepted: bool, reason: str, action: str, proposal: DirectorV2Proposal | None,
    ) -> DirectorV2TakeoverSelection:
        proposal_id = proposal.proposal_id if proposal is not None else ""
        reason = reason[:self._config.max_reason_chars]
        selection = DirectorV2TakeoverSelection(accepted, self._config.stage, reason, action, proposal_id)
        self._sequence += 1
        record_id = f"{proposal_id or 'legacy'}:{action}:{self._sequence}"
        self._records[record_id] = {
            "accepted": accepted, "stage": self._config.stage, "reason_code": reason,
            "action_type": action, "proposal_id": proposal_id,
        }
        while len(self._records) > self._config.max_recent_decisions:
            self._records.popitem(last=False)
        key = f"{self._config.stage}:{reason}"
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_director_v2_takeover"):
            self._metrics.record_director_v2_takeover(self._config.stage, reason, len(self._records))
        return selection


def _clean(value: object) -> str:
    return str(value or "").strip().upper()
