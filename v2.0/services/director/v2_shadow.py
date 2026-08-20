"""Deterministic, read-only Director V2 shadow (Phase 6)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import Capability, CapabilityAvailability
from interfaces.director_v2 import (
    DIRECTOR_V2_SOURCES,
    DirectorV2Candidate,
    DirectorV2Context,
    DirectorV2ContextProvider,
    DirectorV2Proposal,
    DirectorV2ShadowService,
)


_HARD_HOLDS = (
    ("emergency", "emergency"),
    ("operator_hold", "operator_hold"),
    ("safety_hold", "safety_hold"),
    ("permission_hold", "permission_hold"),
    ("transaction_conflict", "transaction_conflict"),
    ("critical_state", "critical_state"),
)
_UNAVAILABLE_REASONS = frozenset({
    "feature_disabled", "unknown_capability", "permission_denied",
    "executor_unhealthy", "missing_verifier", "verifier_unhealthy",
    "transaction_conflict", "world_precondition_failed",
    "self_precondition_failed",
})


@dataclass(frozen=True)
class DirectorV2ShadowConfig:
    tick_seconds: float
    max_recent_records: int
    max_candidates_per_source: int
    max_evidence_refs: int
    max_label_chars: int
    source_weights: Mapping[str, float]
    source_priority: tuple[str, ...]

    def __post_init__(self) -> None:
        tick = _finite_number(self.tick_seconds, "tick_seconds")
        if tick <= 0:
            raise ValueError("tick_seconds must be positive")
        object.__setattr__(self, "tick_seconds", tick)
        for name in (
            "max_recent_records", "max_candidates_per_source",
            "max_evidence_refs", "max_label_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        if not isinstance(self.source_weights, Mapping):
            raise ValueError("source_weights must be a mapping")
        weights: dict[str, float] = {}
        for key, value in self.source_weights.items():
            if not isinstance(key, str) or not key:
                raise ValueError("source_weights keys must be source strings")
            weights[key] = _finite_number(value, f"source_weights.{key}")
        if set(weights) != set(DIRECTOR_V2_SOURCES):
            raise ValueError("source_weights must define every source exactly once")
        if not isinstance(self.source_priority, tuple):
            raise ValueError("source_priority must be a tuple")
        if (
            not all(isinstance(item, str) for item in self.source_priority)
            or len(self.source_priority) != len(DIRECTOR_V2_SOURCES)
            or set(self.source_priority) != set(DIRECTOR_V2_SOURCES)
        ):
            raise ValueError("source_priority must contain every source exactly once")
        object.__setattr__(self, "source_weights", MappingProxyType(weights))

    @classmethod
    def from_loader(cls, loader: Any) -> "DirectorV2ShadowConfig":
        raw = loader.get("director", "director.director_v2_shadow", None)
        if not isinstance(raw, Mapping):
            raise ValueError("director_v2_shadow must be a mapping")
        weights = raw.get("source_weights")
        priority = raw.get("source_priority")
        if not isinstance(weights, Mapping):
            raise ValueError("director_v2_shadow.source_weights must be a mapping")
        if not isinstance(priority, list):
            raise ValueError("director_v2_shadow.source_priority must be a YAML list")
        return cls(
            tick_seconds=raw.get("tick_seconds"),
            max_recent_records=raw.get("max_recent_records"),
            max_candidates_per_source=raw.get("max_candidates_per_source"),
            max_evidence_refs=raw.get("max_evidence_refs"),
            max_label_chars=raw.get("max_label_chars"),
            source_weights=dict(weights),
            source_priority=tuple(priority),
        )


class CandidateGenerationError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class HardArbiter:
    """Resolve non-negotiable holds before donation and soft scoring."""

    def hold_reason(self, context: DirectorV2Context) -> str:
        for attribute, reason in _HARD_HOLDS:
            if getattr(context, attribute) is True:
                return reason
        return ""

    def donation_candidates(
        self, candidates: tuple[DirectorV2Candidate, ...],
    ) -> tuple[DirectorV2Candidate, ...]:
        return tuple(item for item in candidates if item.is_donation)


class CandidateGenerator:
    def __init__(self, config: DirectorV2ShadowConfig) -> None:
        self._config = config

    def generate(self, context: DirectorV2Context) -> tuple[DirectorV2Candidate, ...]:
        max_total = self._config.max_candidates_per_source * len(DIRECTOR_V2_SOURCES)
        if len(context.candidates) > max_total:
            raise CandidateGenerationError("candidate_total_overflow")
        seen: set[tuple[str, str]] = set()
        for item in context.candidates:
            identity = (item.source, item.candidate_id)
            if identity in seen:
                raise CandidateGenerationError("candidate_duplicate")
            seen.add(identity)
            self._validate(item)
        per_source = {source: 0 for source in DIRECTOR_V2_SOURCES}
        values: list[DirectorV2Candidate] = []
        for item in sorted(
            context.candidates,
            key=lambda value: (
                value.source, not value.is_donation, value.candidate_id,
                value.action_type, value.capability_id,
            ),
        ):
            if per_source[item.source] >= self._config.max_candidates_per_source:
                continue
            per_source[item.source] += 1
            values.append(item)
        canonical_wait = DirectorV2Candidate("wait", "wait", "WAIT", "WAIT")
        if canonical_wait not in values:
            values.append(canonical_wait)
        return tuple(values)

    def _validate(self, candidate: DirectorV2Candidate) -> None:
        labels = (
            candidate.candidate_id, candidate.action_type, candidate.capability_id,
            *candidate.evidence_refs,
        )
        if any(len(value) > self._config.max_label_chars for value in labels):
            raise CandidateGenerationError("candidate_label_overflow")
        if len(candidate.evidence_refs) > self._config.max_evidence_refs:
            raise CandidateGenerationError("candidate_evidence_overflow")
        if candidate.source == "wait" and candidate != DirectorV2Candidate(
            "wait", "wait", "WAIT", "WAIT",
        ):
            raise CandidateGenerationError("wait_candidate_invalid")
        if candidate.is_donation and (
            candidate.source != "chat"
            or candidate.action_type != "READ_CHAT"
            or candidate.capability_id != "READ_CHAT"
            or not candidate.evidence_refs
        ):
            raise CandidateGenerationError("donation_candidate_invalid")


class CandidateScorer:
    def __init__(self, config: DirectorV2ShadowConfig) -> None:
        self._weights = config.source_weights
        self._priority = {
            source: index for index, source in enumerate(config.source_priority)
        }

    def rank(
        self, candidates: tuple[DirectorV2Candidate, ...],
    ) -> tuple[tuple[DirectorV2Candidate, float], ...]:
        scored: list[tuple[DirectorV2Candidate, float]] = []
        for item in candidates:
            score = item.score + self._weights[item.source]
            if not math.isfinite(score):
                raise CandidateGenerationError("candidate_score_overflow")
            scored.append((item, score))
        return tuple(sorted(
            scored,
            key=lambda item: (
                -item[1], self._priority[item[0].source], item[0].candidate_id,
                item[0].action_type, item[0].capability_id,
            ),
        ))


class SoftPolicy:
    def select(
        self, ranked: tuple[tuple[DirectorV2Candidate, float], ...],
    ) -> tuple[DirectorV2Candidate, float]:
        if not ranked:
            raise CandidateGenerationError("candidate_set_empty")
        return ranked[0]


class ActionValidator:
    def __init__(self, capability_registry: Any) -> None:
        self._capability_registry = capability_registry

    def validate(self, candidate: DirectorV2Candidate) -> tuple[bool, str]:
        if candidate.action_type == "WAIT" or candidate.capability_id == "WAIT":
            return (
                (True, "wait")
                if candidate.action_type == candidate.capability_id == "WAIT"
                else (False, "wait_capability_mismatch")
            )
        try:
            capability = self._capability_registry.capability(candidate.capability_id)
        except Exception:
            return False, "capability_source_failed"
        if capability is None:
            return False, "capability_unknown"
        if not isinstance(capability, Capability):
            return False, "capability_malformed"
        if capability.capability_id != candidate.capability_id:
            return False, "capability_identity_mismatch"
        if capability.action_type != candidate.action_type:
            return False, "capability_action_mismatch"
        try:
            availability = self._capability_registry.availability(candidate.capability_id)
        except Exception:
            return False, "capability_source_failed"
        if not isinstance(availability, CapabilityAvailability):
            return False, "capability_availability_malformed"
        if availability.capability_id != candidate.capability_id:
            return False, "capability_availability_mismatch"
        if (
            availability.available is True
            and availability.reason_code != "available"
        ) or (
            availability.available is False
            and availability.reason_code not in _UNAVAILABLE_REASONS
        ):
            return False, "capability_availability_malformed"
        if availability.available is not True:
            return False, f"capability_{availability.reason_code}"
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
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, DirectorV2ShadowConfig):
            raise ValueError("config must be DirectorV2ShadowConfig")
        if not callable(context_provider):
            raise ValueError("context_provider must be callable")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._context_provider = context_provider
        self._metrics = metrics
        self._enabled = enabled
        self._clock = clock or time.time
        self._hard_arbiter = HardArbiter()
        self._candidate_generator = CandidateGenerator(config)
        self._candidate_scorer = CandidateScorer(config)
        self._soft_policy = SoftPolicy()
        self._validator = ActionValidator(capability_registry)
        self._records: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._sequence = 0
        self._latest_trace: tuple[DirectorV2Proposal, DirectorV2Context] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "DirectorV2Shadow":
        return cls(DirectorV2ShadowConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        was_enabled = self._enabled
        self._enabled = enabled
        if not enabled:
            self._records.clear()
            self._latest_trace = None
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
        if self._task is None or self._task.done():
            return HealthStatus.degraded(self.service_id, "director v2 shadow worker stopped")
        return HealthStatus.healthy(self.service_id, retained=len(self._records))

    def propose(self, context: DirectorV2Context) -> DirectorV2Proposal:
        if not isinstance(context, DirectorV2Context):
            raise ValueError("context must be DirectorV2Context")
        if not self._enabled:
            return self._proposal(
                context, "WAIT", "WAIT", "wait", ("feature_disabled",), (), 0.0,
            )
        hard_reason = self._hard_arbiter.hold_reason(context)
        if hard_reason:
            return self._safe_wait(context, (hard_reason,), "hard_hold")
        if context.source_failures:
            reason = f"source_{sorted(context.source_failures)[0]}_failed"
            return self._safe_wait(context, (reason,), "source_failed")
        try:
            candidates = self._candidate_generator.generate(context)
            donations = self._hard_arbiter.donation_candidates(candidates)
            if donations:
                selected, score = self._soft_policy.select(
                    self._candidate_scorer.rank(donations),
                )
                reason_codes = ("donation_priority",)
            else:
                selected, score = self._soft_policy.select(
                    self._candidate_scorer.rank(candidates),
                )
                reason_codes = ("selected", f"source_{selected.source}")
        except CandidateGenerationError as exc:
            return self._safe_wait(context, (exc.reason_code,), "candidate_rejected")
        valid, validation_reason = self._validator.validate(selected)
        if not valid:
            return self._safe_wait(
                context, (*reason_codes, validation_reason), "validation_rejected",
            )
        proposal = self._proposal(
            context, selected.action_type, selected.capability_id,
            selected.candidate_id, (*reason_codes, validation_reason),
            selected.evidence_refs, score,
        )
        self._store(proposal, context, "selected")
        return proposal

    def propose_current(self) -> DirectorV2Proposal:
        if not self._enabled:
            return self.propose(DirectorV2Context(
                created_at=self._clock(),
                world_snapshot_id="world-disabled",
                self_snapshot_id="self-disabled",
                capability_snapshot_id="capabilities-disabled",
            ))
        try:
            context = self._context_provider()
            if not isinstance(context, DirectorV2Context):
                raise ValueError("context provider returned an invalid value")
        except Exception:
            context = DirectorV2Context(
                created_at=self._clock(),
                world_snapshot_id="world-unavailable",
                self_snapshot_id="self-unavailable",
                capability_snapshot_id="capabilities-unavailable",
                source_failures=("context",),
            )
        return self.propose(context)

    def trajectory_context(self, proposal_id: str) -> DirectorV2Context | None:
        trace = self._latest_trace
        if trace is None or trace[0].proposal_id != str(proposal_id):
            return None
        return trace[1]

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
                self.propose_current()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record("proposal_failed")
            await asyncio.sleep(self._config.tick_seconds)

    def _safe_wait(
        self, context: DirectorV2Context, reason_codes: tuple[str, ...], outcome: str,
    ) -> DirectorV2Proposal:
        proposal = self._proposal(
            context, "WAIT", "WAIT", "wait", reason_codes, (), 0.0,
        )
        self._store(proposal, context, outcome)
        return proposal

    def _proposal(
        self,
        context: DirectorV2Context,
        action_type: str,
        capability_id: str,
        candidate_id: str,
        reason_codes: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        score: float,
    ) -> DirectorV2Proposal:
        action = self._label(action_type, "action_type")
        capability = self._label(capability_id, "capability_id")
        candidate = self._label(candidate_id, "candidate_id")
        reasons = tuple(self._label(item, "reason_code") for item in reason_codes)
        clean_evidence = self._bounded_evidence(evidence_refs)
        clean_score = _finite_number(score, "proposal score")
        payload = {
            "created_at": context.created_at,
            "world": context.world_snapshot_id,
            "self": context.self_snapshot_id,
            "capability": context.capability_snapshot_id,
            "action": action,
            "capability_id": capability,
            "candidate_id": candidate,
            "reasons": reasons,
            "evidence": clean_evidence,
            "score": clean_score,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        return DirectorV2Proposal(
            proposal_id=f"d2_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}",
            created_at=context.created_at,
            action_type=action,
            capability_id=capability,
            candidate_id=candidate,
            reason_codes=reasons,
            evidence_refs=clean_evidence,
            score=clean_score,
        )

    def _store(
        self, proposal: DirectorV2Proposal, context: DirectorV2Context, outcome: str,
    ) -> None:
        self._latest_trace = (proposal, context)
        record = {
            "proposal_id": proposal.proposal_id,
            "created_at": proposal.created_at,
            "world_snapshot_id": self._label(
                context.world_snapshot_id, "world_snapshot_id",
            ),
            "self_snapshot_id": self._label(
                context.self_snapshot_id, "self_snapshot_id",
            ),
            "capability_snapshot_id": self._label(
                context.capability_snapshot_id, "capability_snapshot_id",
            ),
            "action_type": proposal.action_type,
            "capability_id": proposal.capability_id,
            "candidate_id": proposal.candidate_id,
            "reason_codes": proposal.reason_codes,
            "evidence_refs": proposal.evidence_refs,
            "score": proposal.score,
            "outcome": outcome,
        }
        self._sequence += 1
        self._records[self._sequence] = record
        while len(self._records) > self._config.max_recent_records:
            self._records.popitem(last=False)
        self._record(outcome)

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        try:
            recorder = getattr(self._metrics, "record_director_v2_shadow", None)
            if callable(recorder):
                recorder(outcome, len(self._records))
        except Exception:
            pass

    def _bounded_evidence(self, values: tuple[str, ...]) -> tuple[str, ...]:
        result = tuple(sorted(set(values)))
        if len(result) > self._config.max_evidence_refs:
            raise ValueError("evidence_refs exceed configured bound")
        return tuple(self._label(value, "evidence_ref") for value in result)

    def _label(self, value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        clean = value.strip()
        if len(clean) > self._config.max_label_chars:
            raise ValueError(f"{field_name} exceeds configured bound")
        return clean


def director_v2_snapshot_id(prefix: str, value: object) -> str:
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("snapshot prefix must be a non-empty string")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return f"{prefix.strip()}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result
