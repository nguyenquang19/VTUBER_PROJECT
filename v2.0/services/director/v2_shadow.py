"""Deterministic, read-only Director V2 shadow (Phase 6)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.director_v2 import (
    DirectorV2Candidate,
    DirectorV2Context,
    DirectorV2ContextProvider,
    DirectorV2Proposal,
    DirectorV2ShadowService,
)


_SOURCES = ("chat", "thread", "goal", "world", "capability", "proactive", "wait")
_HARD_HOLDS = (
    ("emergency", "emergency"),
    ("operator_hold", "operator_hold"),
    ("safety_hold", "safety_hold"),
    ("permission_hold", "permission_hold"),
    ("transaction_conflict", "transaction_conflict"),
    ("critical_state", "critical_state"),
)


@dataclass(frozen=True)
class DirectorV2ShadowConfig:
    tick_seconds: float
    max_recent_records: int
    max_candidates_per_source: int
    max_evidence_refs: int
    max_label_chars: int
    source_weights: Mapping[str, float]
    source_priority: tuple[str, ...]

    @classmethod
    def from_loader(cls, loader: Any) -> "DirectorV2ShadowConfig":
        raw = loader.get("director", "director.director_v2_shadow", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("director_v2_shadow must be a mapping")
        weights = raw.get("source_weights", {})
        priority = tuple(str(value).strip() for value in raw.get("source_priority", ()) if str(value).strip())
        config = cls(
            tick_seconds=float(raw.get("tick_seconds", 0)),
            max_recent_records=int(raw.get("max_recent_records", 0)),
            max_candidates_per_source=int(raw.get("max_candidates_per_source", 0)),
            max_evidence_refs=int(raw.get("max_evidence_refs", 0)),
            max_label_chars=int(raw.get("max_label_chars", 0)),
            source_weights={str(key): float(value) for key, value in dict(weights).items()},
            source_priority=priority,
        )
        if min(
            config.tick_seconds, config.max_recent_records,
            config.max_candidates_per_source, config.max_evidence_refs,
            config.max_label_chars,
        ) <= 0:
            raise ValueError("director_v2_shadow bounds must be positive")
        if set(config.source_weights) != set(_SOURCES):
            raise ValueError("director_v2_shadow.source_weights must define every source")
        if set(config.source_priority) != set(_SOURCES) or len(config.source_priority) != len(_SOURCES):
            raise ValueError("director_v2_shadow.source_priority must contain every source once")
        return config


class HardArbiter:
    """Resolve non-negotiable holds before soft scoring."""

    def decide(self, context: DirectorV2Context) -> tuple[str, DirectorV2Candidate | None]:
        for attribute, reason in _HARD_HOLDS:
            if bool(getattr(context, attribute)):
                return reason, None
        donation = next((item for item in context.candidates if item.is_donation), None)
        return ("donation_priority", donation) if donation is not None else ("", None)


class CandidateGenerator:
    def __init__(self, config: DirectorV2ShadowConfig) -> None:
        self._config = config

    def generate(self, context: DirectorV2Context) -> tuple[DirectorV2Candidate, ...]:
        per_source: dict[str, int] = {source: 0 for source in _SOURCES}
        values: list[DirectorV2Candidate] = []
        for item in sorted(context.candidates, key=lambda value: (value.source, value.candidate_id)):
            if item.source not in per_source or not str(item.candidate_id).strip():
                continue
            if per_source[item.source] >= self._config.max_candidates_per_source:
                continue
            per_source[item.source] += 1
            values.append(item)
        if not any(item.source == "wait" for item in values):
            values.append(DirectorV2Candidate("wait", "wait", "WAIT", "WAIT"))
        return tuple(values)


class CandidateScorer:
    def __init__(self, config: DirectorV2ShadowConfig) -> None:
        self._weights = config.source_weights
        self._priority = {source: index for index, source in enumerate(config.source_priority)}

    def rank(self, candidates: tuple[DirectorV2Candidate, ...]) -> tuple[tuple[DirectorV2Candidate, float], ...]:
        scored = tuple((item, float(item.score) + self._weights[item.source]) for item in candidates)
        return tuple(sorted(
            scored,
            key=lambda item: (-item[1], self._priority[item[0].source], item[0].candidate_id),
        ))


class SoftPolicy:
    """The configured source weights are applied by CandidateScorer only."""

    def select(self, ranked: tuple[tuple[DirectorV2Candidate, float], ...]) -> tuple[DirectorV2Candidate, float]:
        return ranked[0]


class ActionValidator:
    def __init__(self, capability_registry: Any) -> None:
        self._capability_registry = capability_registry

    def validate(self, candidate: DirectorV2Candidate) -> tuple[bool, str]:
        if candidate.action_type == "WAIT":
            return True, "wait"
        if not candidate.capability_id:
            return False, "missing_capability"
        try:
            availability = self._capability_registry.availability(candidate.capability_id)
        except Exception:
            return False, "capability_unknown"
        if not bool(getattr(availability, "available", False)):
            reason = str(getattr(availability, "reason_code", "unavailable"))
            return False, f"capability_{reason}"
        return True, "validated"


class DirectorV2Shadow(DirectorV2ShadowService):
    service_id = "director_v2_shadow"

    def __init__(
        self,
        config: DirectorV2ShadowConfig,
        *,
        capability_registry: Any,
        context_provider: DirectorV2ContextProvider,
        metrics: Any = None,
        enabled: bool = True,
    ) -> None:
        self._config = config
        self._context_provider = context_provider
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._hard_arbiter = HardArbiter()
        self._candidate_generator = CandidateGenerator(config)
        self._candidate_scorer = CandidateScorer(config)
        self._soft_policy = SoftPolicy()
        self._validator = ActionValidator(capability_registry)
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "DirectorV2Shadow":
        return cls(DirectorV2ShadowConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        was_enabled = self._enabled
        self._enabled = bool(enabled)
        if not self._enabled:
            self._records.clear()
            if self._task is not None:
                self._task.cancel()
                self._task = None
        elif self._running and not was_enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="director_v2_shadow")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._enabled:
            self._task = asyncio.create_task(self._loop(), name="director_v2_shadow")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "director v2 shadow disabled")
        return HealthStatus.healthy(self.service_id, retained=len(self._records))

    def propose(self, context: DirectorV2Context) -> DirectorV2Proposal:
        if not self._enabled:
            return self._proposal(context, "WAIT", "WAIT", "wait", ("feature_disabled",), (), 0.0)
        hard_reason, hard_candidate = self._hard_arbiter.decide(context)
        if hard_reason and hard_candidate is None:
            proposal = self._proposal(context, "WAIT", "WAIT", "wait", (hard_reason,), (), 0.0)
            self._store(proposal, context, "hard_hold")
            return proposal
        candidates = self._candidate_generator.generate(context)
        if hard_candidate is not None:
            selected, score = hard_candidate, float(hard_candidate.score)
            reason_codes = (hard_reason,)
        else:
            selected, score = self._soft_policy.select(self._candidate_scorer.rank(candidates))
            reason_codes = ("selected", f"source_{selected.source}")
        valid, validation_reason = self._validator.validate(selected)
        if not valid:
            proposal = self._proposal(context, "WAIT", "WAIT", "wait", (*reason_codes, validation_reason), (), 0.0)
            self._store(proposal, context, "validation_rejected")
            return proposal
        proposal = self._proposal(
            context, selected.action_type, selected.capability_id, selected.candidate_id,
            (*reason_codes, validation_reason), selected.evidence_refs, score,
        )
        self._store(proposal, context, "selected")
        return proposal

    def propose_current(self) -> DirectorV2Proposal:
        """Use the public composition-root provider; never execute an action."""
        return self.propose(self._context_provider())

    def snapshot(self) -> dict[str, object]:
        recent = list(self._records.values())[-self._config.max_recent_records:]
        return {
            "enabled": self._enabled,
            "counts": dict(sorted(self._counts.items())),
            "current": recent[-1] if recent else None,
            "recent": list(reversed(recent)),
        }

    def get_metrics(self) -> dict[str, Any]:
        return {
            "director_v2_shadow_enabled": self._enabled,
            "director_v2_shadow_retained": len(self._records),
            "director_v2_shadow_outcomes": dict(sorted(self._counts.items())),
        }

    async def _loop(self) -> None:
        while self._running and self._enabled:
            try:
                self.propose(self._context_provider())
            except Exception:
                self._record("context_failed")
            await asyncio.sleep(self._config.tick_seconds)

    def _proposal(
        self, context: DirectorV2Context, action_type: str, capability_id: str,
        candidate_id: str, reason_codes: tuple[str, ...], evidence_refs: tuple[str, ...], score: float,
    ) -> DirectorV2Proposal:
        clean_evidence = self._bounded(evidence_refs)
        payload = {
            "created_at": float(context.created_at), "world": context.world_snapshot_id,
            "self": context.self_snapshot_id, "capability": context.capability_snapshot_id,
            "action": action_type, "capability_id": capability_id, "candidate_id": candidate_id,
            "reasons": reason_codes, "evidence": clean_evidence, "score": score,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return DirectorV2Proposal(
            proposal_id=f"d2_{hashlib.sha256(encoded.encode()).hexdigest()[:16]}",
            created_at=float(context.created_at), action_type=self._label(action_type),
            capability_id=self._label(capability_id), candidate_id=self._label(candidate_id),
            reason_codes=tuple(self._label(item) for item in reason_codes),
            evidence_refs=clean_evidence, score=float(score),
        )

    def _store(self, proposal: DirectorV2Proposal, context: DirectorV2Context, outcome: str) -> None:
        record = {
            "proposal_id": proposal.proposal_id, "created_at": proposal.created_at,
            "world_snapshot_id": self._label(context.world_snapshot_id),
            "self_snapshot_id": self._label(context.self_snapshot_id),
            "capability_snapshot_id": self._label(context.capability_snapshot_id),
            "action_type": proposal.action_type, "capability_id": proposal.capability_id,
            "candidate_id": proposal.candidate_id, "reason_codes": proposal.reason_codes,
            "evidence_refs": proposal.evidence_refs, "score": proposal.score, "outcome": outcome,
        }
        self._records[proposal.proposal_id] = record
        self._records.move_to_end(proposal.proposal_id)
        while len(self._records) > self._config.max_recent_records:
            self._records.popitem(last=False)
        self._record(outcome)

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_director_v2_shadow"):
            self._metrics.record_director_v2_shadow(outcome, len(self._records))

    def _bounded(self, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            clean = self._label(value)
            if clean and clean not in result:
                result.append(clean)
            if len(result) >= self._config.max_evidence_refs:
                break
        return tuple(result)

    def _label(self, value: object) -> str:
        return str(value).strip()[:self._config.max_label_chars]
