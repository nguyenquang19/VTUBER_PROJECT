"""Bounded structured Director trajectory recorder and deterministic replay."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Mapping

from interfaces.action_execution import VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.director_v2 import DirectorV2Context, DirectorV2Proposal
from interfaces.trajectory import (
    TrajectoryProposer,
    TrajectoryRecordService,
    TrajectoryReplayResult,
    TrajectorySnapshotRefs,
)


_CONFIG_KEYS = {
    "schema_version", "max_recent", "dashboard_recent", "max_candidates",
    "max_evidence_refs", "max_reason_codes", "max_label_chars",
}
_TERMINAL = {"shadow_only", "completed", "no_action", "incomplete"}


@dataclass(frozen=True)
class TrajectoryConfig:
    schema_version: int
    max_recent: int
    dashboard_recent: int
    max_candidates: int
    max_evidence_refs: int
    max_reason_codes: int
    max_label_chars: int

    @classmethod
    def from_loader(cls, loader: Any) -> "TrajectoryConfig":
        raw = loader.get("director", "director.trajectory_records", None)
        if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS:
            raise ValueError("director.trajectory_records keys are invalid")
        config = cls(**{
            key: _strict_positive_int(raw[key], key) for key in _CONFIG_KEYS
        })
        if config.dashboard_recent > config.max_recent:
            raise ValueError("trajectory dashboard_recent cannot exceed max_recent")
        return config


@dataclass(frozen=True)
class _StoredTrajectory:
    schema_version: int
    trajectory_id: str
    created_at: float
    updated_at: float
    initial: TrajectorySnapshotRefs
    context: DirectorV2Context
    proposal: DirectorV2Proposal
    owner: Literal["unselected", "legacy", "director_v2"] = "unselected"
    lifecycle: str = "proposed"
    action_request: Mapping[str, Any] | None = None
    action_result: Mapping[str, Any] | None = None
    verification: Mapping[str, Any] | None = None
    next_snapshot: TrajectorySnapshotRefs | None = None
    terminal_reason: str = ""
    fingerprint: str = ""


class TrajectoryRecorder(TrajectoryRecordService):
    service_id = "trajectory_records"

    def __init__(
        self,
        config: TrajectoryConfig,
        *,
        snapshot_provider: Callable[[], TrajectorySnapshotRefs],
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        enabled: bool = False,
    ) -> None:
        if not isinstance(config, TrajectoryConfig):
            raise ValueError("config must be TrajectoryConfig")
        if not callable(snapshot_provider):
            raise ValueError("snapshot_provider must be callable")
        self.config = config
        self._snapshot_provider = snapshot_provider
        self._clock = clock or time.time
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._items: OrderedDict[str, _StoredTrajectory] = OrderedDict()
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        snapshot_provider: Callable[[], TrajectorySnapshotRefs],
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        enabled: bool = False,
    ) -> "TrajectoryRecorder":
        return cls(
            TrajectoryConfig.from_loader(loader),
            snapshot_provider=snapshot_provider,
            clock=clock,
            metrics=metrics,
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._finalize_open("service_stopped")
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, retained=len(self._items),
            open=sum(item.lifecycle not in _TERMINAL for item in self._items.values()),
        )

    def set_enabled(self, enabled: bool) -> None:
        value = bool(enabled)
        if self.enabled and not value:
            self._finalize_open("feature_disabled")
        self.enabled = value

    def begin(self, context: DirectorV2Context, proposal: DirectorV2Proposal) -> str | None:
        if not self.enabled:
            return None
        if not isinstance(context, DirectorV2Context) or not isinstance(
            proposal, DirectorV2Proposal,
        ):
            raise ValueError("trajectory requires typed Director V2 context and proposal")
        if proposal.proposal_id in self._items:
            raise ValueError("trajectory proposal_id must be unique")
        self._validate_context(context)
        self._validate_proposal(proposal)
        if proposal.created_at != context.created_at:
            raise ValueError("trajectory context and proposal timestamps disagree")
        if proposal.candidate_id != "wait" and not any(
            item.candidate_id == proposal.candidate_id
            and item.action_type == proposal.action_type
            and item.capability_id == proposal.capability_id
            for item in context.candidates
        ):
            raise ValueError("trajectory selected candidate is absent from context")
        initial = TrajectorySnapshotRefs(
            context.world_snapshot_id,
            context.self_snapshot_id,
            context.capability_snapshot_id,
        )
        record = _StoredTrajectory(
            schema_version=self.config.schema_version,
            trajectory_id=proposal.proposal_id,
            created_at=context.created_at,
            updated_at=context.created_at,
            initial=initial,
            context=context,
            proposal=proposal,
        )
        record = replace(record, fingerprint=self._fingerprint(record))
        self._items[record.trajectory_id] = record
        self._trim()
        self._record("proposed")
        return record.trajectory_id

    def mark_selection(
        self, trajectory_id: str, *, owner: Literal["legacy", "director_v2"],
    ) -> None:
        record = self._get_open(trajectory_id)
        if record is None:
            return
        if owner not in {"legacy", "director_v2"}:
            raise ValueError("trajectory owner is invalid")
        lifecycle = "shadow_only" if owner == "legacy" else "owned"
        next_snapshot = self._next_snapshot() if owner == "legacy" else None
        updated = replace(
            record,
            owner=owner,
            lifecycle=lifecycle,
            updated_at=self._now(),
            next_snapshot=next_snapshot,
            terminal_reason="legacy_owned" if owner == "legacy" else "",
            fingerprint="",
        )
        self._store(replace(updated, fingerprint=self._fingerprint(updated)))
        self._record(lifecycle)

    def record_action(self, trajectory_id: str, request: ActionRequest) -> None:
        record = self._get_open(trajectory_id)
        if record is None:
            return
        if record.owner != "director_v2" or record.lifecycle != "owned":
            raise ValueError("trajectory action requires Director V2 ownership")
        if not isinstance(request, ActionRequest):
            raise ValueError("trajectory action requires ActionRequest")
        projection = self._request_projection(request)
        updated = replace(
            record,
            lifecycle="action_requested",
            action_request=projection,
            updated_at=self._now(),
            fingerprint="",
        )
        self._store(replace(updated, fingerprint=self._fingerprint(updated)))
        self._record("action_requested")

    def record_result(
        self, trajectory_id: str, result: ActionResult,
        verification: VerificationResult,
    ) -> None:
        record = self._get_open(trajectory_id)
        if record is None:
            return
        if record.lifecycle != "action_requested" or record.action_request is None:
            raise ValueError("trajectory result requires one action request")
        if not isinstance(result, ActionResult) or not isinstance(
            verification, VerificationResult,
        ):
            raise ValueError("trajectory result requires typed result and verification")
        if result.action_id != record.action_request["action_id"]:
            raise ValueError("trajectory action result ID mismatch")
        if result.verified != verification.verified:
            raise ValueError("trajectory verification flags disagree")
        if result.verified and result.verification_source != verification.source:
            raise ValueError("trajectory verification sources disagree")
        result_projection = self._result_projection(result)
        verification_projection = {
            "verified": verification.verified,
            "source": self._label(verification.source, "verification source"),
            "reason_code": self._label(
                verification.reason_code, "verification reason",
            ),
            "evidence_refs": list(self._bounded_refs(verification.evidence_refs)),
        }
        updated = replace(
            record,
            lifecycle="completed",
            action_result=result_projection,
            verification=verification_projection,
            next_snapshot=self._next_snapshot(),
            terminal_reason=verification.reason_code,
            updated_at=self._now(),
            fingerprint="",
        )
        self._store(replace(updated, fingerprint=self._fingerprint(updated)))
        self._record("completed")

    def record_no_action(self, trajectory_id: str, *, reason_code: str) -> None:
        record = self._get_open(trajectory_id)
        if record is None:
            return
        if record.owner != "director_v2" or record.action_request is not None:
            raise ValueError("no-action trajectory requires owned decision without request")
        reason = self._label(reason_code, "no-action reason")
        updated = replace(
            record,
            lifecycle="no_action",
            next_snapshot=self._next_snapshot(),
            terminal_reason=reason,
            updated_at=self._now(),
            fingerprint="",
        )
        self._store(replace(updated, fingerprint=self._fingerprint(updated)))
        self._record("no_action")

    def replay(
        self, trajectory_id: str, proposer: TrajectoryProposer,
    ) -> TrajectoryReplayResult:
        record = self._items.get(str(trajectory_id))
        if record is None:
            raise KeyError(str(trajectory_id))
        if not callable(proposer):
            raise ValueError("trajectory proposer must be callable")
        replayed = proposer(record.context)
        if not isinstance(replayed, DirectorV2Proposal):
            raise ValueError("trajectory proposer returned an invalid proposal")
        mismatches = tuple(
            name for name, expected, actual in (
                ("action_type", record.proposal.action_type, replayed.action_type),
                ("capability_id", record.proposal.capability_id, replayed.capability_id),
                ("candidate_id", record.proposal.candidate_id, replayed.candidate_id),
                ("reason_codes", record.proposal.reason_codes, replayed.reason_codes),
                ("evidence_refs", record.proposal.evidence_refs, replayed.evidence_refs),
                ("score", record.proposal.score, replayed.score),
            )
            if expected != actual
        )
        outcome = "replay_match" if not mismatches else "replay_mismatch"
        self._record(outcome)
        return TrajectoryReplayResult(
            trajectory_id=record.trajectory_id,
            matched=not mismatches,
            mismatches=mismatches,
            fingerprint=record.fingerprint,
        )

    def snapshot(self) -> dict[str, Any]:
        recent = list(self._items.values())[-self.config.dashboard_recent:]
        projection = {
            "schema_version": self.config.schema_version,
            "enabled": self.enabled,
            "running": self._running,
            "counts": dict(sorted(self._counts.items())),
            "current": self._public(recent[-1]) if recent else None,
            "recent": [self._public(item) for item in reversed(recent)],
        }
        return json.loads(json.dumps(projection, ensure_ascii=False))

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"trajectory_{outcome}_total": count
            for outcome, count in sorted(self._counts.items())
        }

    def _get_open(self, trajectory_id: str) -> _StoredTrajectory | None:
        if not self.enabled:
            return None
        record = self._items.get(str(trajectory_id))
        if record is None:
            raise KeyError(str(trajectory_id))
        if record.lifecycle in _TERMINAL:
            raise ValueError("trajectory is already terminal")
        return record

    def _store(self, record: _StoredTrajectory) -> None:
        self._items[record.trajectory_id] = record
        self._items.move_to_end(record.trajectory_id)

    def _validate_context(self, context: DirectorV2Context) -> None:
        if len(context.candidates) > self.config.max_candidates:
            raise ValueError("trajectory candidates exceed configured bound")
        self._label(context.world_snapshot_id, "world snapshot")
        self._label(context.self_snapshot_id, "self snapshot")
        self._label(context.capability_snapshot_id, "capability snapshot")
        for candidate in context.candidates:
            for value, name in (
                (candidate.source, "candidate source"),
                (candidate.candidate_id, "candidate ID"),
                (candidate.action_type, "candidate action"),
                (candidate.capability_id, "candidate capability"),
            ):
                self._label(value, name)
            self._bounded_refs(candidate.evidence_refs)

    def _validate_proposal(self, proposal: DirectorV2Proposal) -> None:
        for value, name in (
            (proposal.proposal_id, "proposal ID"),
            (proposal.action_type, "proposal action"),
            (proposal.capability_id, "proposal capability"),
            (proposal.candidate_id, "proposal candidate"),
        ):
            self._label(value, name)
        if len(proposal.reason_codes) > self.config.max_reason_codes:
            raise ValueError("trajectory reason codes exceed configured bound")
        for reason in proposal.reason_codes:
            self._label(reason, "reason code")
        self._bounded_refs(proposal.evidence_refs)

    def _request_projection(self, request: ActionRequest) -> dict[str, Any]:
        argument_keys = tuple(
            self._label(key, "argument key") for key in request.arguments
        )
        return {
            "schema_version": request.schema_version,
            "action_id": self._label(request.action_id, "action ID"),
            "capability_id": self._label(request.capability_id, "capability ID"),
            "action_type": self._label(request.action_type, "action type"),
            "target_present": request.target is not None,
            "argument_keys": list(argument_keys),
            "intention_id": (
                self._label(request.intention_id, "intention ID")
                if request.intention_id is not None else None
            ),
            "evidence_refs": list(self._bounded_refs(request.evidence_refs)),
            "idempotency_key": self._label(request.idempotency_key, "idempotency key"),
            "priority": request.priority,
            "requested_at": request.requested_at.isoformat(),
            "transaction_policy": self._label(
                request.transaction_policy, "transaction policy",
            ),
        }

    def _result_projection(self, result: ActionResult) -> dict[str, Any]:
        result_keys = tuple(self._label(key, "result key") for key in result.result_data)
        return {
            "schema_version": result.schema_version,
            "action_id": self._label(result.action_id, "action result ID"),
            "status": result.status.value,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
            "verified": result.verified,
            "verification_source": result.verification_source,
            "result_keys": list(result_keys),
            "error_code": result.error_code,
        }

    def _next_snapshot(self) -> TrajectorySnapshotRefs:
        value = self._snapshot_provider()
        if not isinstance(value, TrajectorySnapshotRefs):
            raise ValueError("trajectory snapshot provider returned an invalid value")
        for item in value.to_dict().values():
            self._label(item, "next snapshot")
        return value

    def _finalize_open(self, reason: str) -> None:
        for record in tuple(self._items.values()):
            if record.lifecycle in _TERMINAL:
                continue
            try:
                next_snapshot = self._next_snapshot()
            except Exception:
                next_snapshot = TrajectorySnapshotRefs(
                    "world-unavailable", "self-unavailable", "capabilities-unavailable",
                )
            updated = replace(
                record,
                lifecycle="incomplete",
                next_snapshot=next_snapshot,
                terminal_reason=reason,
                updated_at=self._now(),
                fingerprint="",
            )
            self._store(replace(updated, fingerprint=self._fingerprint(updated)))
            self._record("incomplete")

    def _public(self, record: _StoredTrajectory) -> dict[str, Any]:
        context = record.context
        return {
            "schema_version": record.schema_version,
            "trajectory_id": record.trajectory_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "initial_snapshot": record.initial.to_dict(),
            "decision": {
                "candidates": [
                    {
                        "source": item.source,
                        "candidate_id": item.candidate_id,
                        "action_type": item.action_type,
                        "capability_id": item.capability_id,
                        "score": item.score,
                        "evidence_refs": list(item.evidence_refs),
                        "is_donation": item.is_donation,
                    }
                    for item in context.candidates
                ],
                "selected_action": record.proposal.action_type,
                "selected_capability": record.proposal.capability_id,
                "selected_candidate": record.proposal.candidate_id,
                "reason_codes": list(record.proposal.reason_codes),
                "evidence_refs": list(record.proposal.evidence_refs),
                "score": record.proposal.score,
                "flags": {
                    "emergency": context.emergency,
                    "operator_hold": context.operator_hold,
                    "safety_hold": context.safety_hold,
                    "permission_hold": context.permission_hold,
                    "transaction_conflict": context.transaction_conflict,
                    "critical_state": context.critical_state,
                    "source_failures": list(context.source_failures),
                },
            },
            "owner": record.owner,
            "lifecycle": record.lifecycle,
            "action_request": dict(record.action_request) if record.action_request else None,
            "action_result": dict(record.action_result) if record.action_result else None,
            "verification": dict(record.verification) if record.verification else None,
            "next_snapshot": (
                record.next_snapshot.to_dict() if record.next_snapshot else None
            ),
            "terminal_reason": record.terminal_reason,
            "fingerprint": record.fingerprint,
            "chain_of_thought_included": False,
            "raw_prompt_included": False,
            "raw_memory_included": False,
            "raw_action_values_included": False,
        }

    def _fingerprint(self, record: _StoredTrajectory) -> str:
        value = self._public(replace(record, fingerprint=""))
        value.pop("fingerprint", None)
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def _bounded_refs(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > self.config.max_evidence_refs:
            raise ValueError("trajectory evidence exceeds configured bound")
        result = tuple(self._label(item, "evidence reference") for item in values)
        if len(result) != len(set(result)):
            raise ValueError("trajectory evidence references must be unique")
        return result

    def _label(self, value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        text = value.strip()
        if len(text) > self.config.max_label_chars:
            raise ValueError(f"{name} exceeds configured bound")
        return text

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("trajectory clock must return a finite number")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError("trajectory clock must return a finite number")
        return result

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_trajectory"):
            try:
                self._metrics.record_trajectory(outcome)
            except Exception:
                pass

    def _trim(self) -> None:
        while len(self._items) > self.config.max_recent:
            self._items.popitem(last=False)


def _strict_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
