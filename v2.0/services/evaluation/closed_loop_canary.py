"""Operator-triggered Phase 15 closed-loop canary over the verified action boundary."""
from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.execution import VerificationResult
from interfaces.base import HealthStatus
from interfaces.director_v2 import DirectorV2Context, DirectorV2Proposal
from interfaces.release_readiness import ClosedLoopCanaryRecord, ClosedLoopCanaryService
from interfaces.trajectory import TrajectorySnapshotRefs
from services.evaluation.release_gate import SourceState


_CONFIG_KEYS = {
    "schema_version", "allowed_actions", "execution_timeout_s",
    "max_recent", "max_label_chars",
}


@dataclass(frozen=True)
class ClosedLoopCanaryConfig:
    schema_version: int
    allowed_actions: tuple[str, ...]
    execution_timeout_s: float
    max_recent: int
    max_label_chars: int

    @classmethod
    def from_loader(cls, loader: Any) -> "ClosedLoopCanaryConfig":
        raw = loader.get("operations", "closed_loop_canary", None)
        if not isinstance(raw, Mapping) or set(raw) != _CONFIG_KEYS:
            raise ValueError("closed_loop_canary keys are invalid")
        actions = raw["allowed_actions"]
        if not isinstance(actions, list) or not actions or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in actions
        ):
            raise ValueError("closed_loop_canary.allowed_actions is invalid")
        normalized = tuple(actions)
        if len(normalized) != len(set(normalized)):
            raise ValueError("closed_loop_canary.allowed_actions must be unique")
        schema = raw["schema_version"]
        max_recent = raw["max_recent"]
        max_label = raw["max_label_chars"]
        timeout = raw["execution_timeout_s"]
        if type(schema) is not int or schema != 1:
            raise ValueError("closed_loop_canary.schema_version must be 1")
        if type(max_recent) is not int or max_recent <= 0:
            raise ValueError("closed_loop_canary.max_recent must be positive")
        if type(max_label) is not int or max_label <= 0:
            raise ValueError("closed_loop_canary.max_label_chars must be positive")
        if (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or float(timeout) <= 0
        ):
            raise ValueError("closed_loop_canary.execution_timeout_s must be positive")
        return cls(schema, normalized, float(timeout), max_recent, max_label)


ContextProvider = Callable[[], DirectorV2Context]
ProposalProvider = Callable[[DirectorV2Context], DirectorV2Proposal]
ActionExecutor = Callable[[ActionRequest], Awaitable[ActionResult]]
SourceStateProvider = Callable[[], SourceState]
Clock = Callable[[], datetime]


class ClosedLoopCanary(ClosedLoopCanaryService):
    service_id = "closed_loop_canary"

    def __init__(
        self,
        config: ClosedLoopCanaryConfig,
        *,
        current_product_version: str,
        target_product_version: str,
        context_provider: ContextProvider,
        proposal_provider: ProposalProvider,
        action_executor: ActionExecutor,
        source_state_provider: SourceStateProvider,
        trajectory_records: Any = None,
        metrics: Any = None,
        clock: Clock | None = None,
        enabled: bool = False,
    ) -> None:
        if not isinstance(config, ClosedLoopCanaryConfig):
            raise ValueError("config must be ClosedLoopCanaryConfig")
        for value, name in (
            (current_product_version, "current_product_version"),
            (target_product_version, "target_product_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for value, name in (
            (context_provider, "context_provider"),
            (proposal_provider, "proposal_provider"),
            (action_executor, "action_executor"),
            (source_state_provider, "source_state_provider"),
        ):
            if not callable(value):
                raise ValueError(f"{name} must be callable")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        self.config = config
        self.current_product_version = current_product_version.strip()
        self.target_product_version = target_product_version.strip()
        self._context_provider = context_provider
        self._proposal_provider = proposal_provider
        self._action_executor = action_executor
        self._source_state_provider = source_state_provider
        self._trajectory_records = trajectory_records
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = enabled
        self._running = False
        self._sequence = 0
        self._records: deque[ClosedLoopCanaryRecord] = deque(maxlen=config.max_recent)
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "ClosedLoopCanary":
        return cls(ClosedLoopCanaryConfig.from_loader(loader), **kwargs)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self.enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        return HealthStatus.healthy(self.service_id, recent=len(self._records))

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        self.enabled = enabled

    async def run(self, request: ActionRequest) -> ClosedLoopCanaryRecord:
        if not isinstance(request, ActionRequest):
            raise ValueError("canary request must be ActionRequest")
        async with self._lock:
            return await self._run_once(request)

    async def _run_once(self, request: ActionRequest) -> ClosedLoopCanaryRecord:
        if not self._running:
            self._record_metric("service_stopped")
            raise RuntimeError("closed-loop canary service is stopped")
        if not self.enabled:
            self._record_metric("feature_disabled")
            raise RuntimeError("closed-loop canary feature is disabled")
        if request.action_type not in self.config.allowed_actions:
            self._record_metric("action_not_allowed")
            raise ValueError("canary action is not allowlisted")
        source = await asyncio.to_thread(self._source_state_provider)
        if not isinstance(source, SourceState) or not source.clean:
            self._record_metric("source_not_clean")
            raise RuntimeError("closed-loop canary requires a clean Git source revision")
        started = self._utc_now()
        pre = self._context_provider()
        if not isinstance(pre, DirectorV2Context):
            raise ValueError("canary context provider returned an invalid context")
        proposal = self._proposal_provider(pre)
        if not isinstance(proposal, DirectorV2Proposal):
            raise ValueError("canary proposal provider returned an invalid proposal")
        if (
            proposal.action_type != request.action_type
            or proposal.capability_id != request.capability_id
            or not any(
                candidate.candidate_id == proposal.candidate_id
                and candidate.action_type == request.action_type
                and candidate.capability_id == request.capability_id
                for candidate in pre.candidates
            )
        ):
            self._record_metric("proposal_mismatch")
            raise RuntimeError("closed-loop canary proposal does not own the requested action")

        trajectory_id: str | None = None
        if self._trajectory_records is not None:
            trajectory_id = self._trajectory_records.begin(pre, proposal)
            if trajectory_id is not None:
                self._trajectory_records.mark_selection(
                    trajectory_id, owner="director_v2",
                )
                self._trajectory_records.record_action(trajectory_id, request)
        try:
            result = await asyncio.wait_for(
                self._action_executor(request), timeout=self.config.execution_timeout_s,
            )
        except asyncio.CancelledError:
            self._record_metric("cancelled")
            raise
        except asyncio.TimeoutError as exc:
            self._record_metric("timeout")
            raise RuntimeError("closed-loop canary execution timed out") from exc
        if not isinstance(result, ActionResult) or result.action_id != request.action_id:
            self._record_metric("invalid_result")
            raise RuntimeError("closed-loop canary received an invalid action result")

        verified = result.verified is True and result.status.value == "success"
        world_projected = result.result_data.get("world_projected") is True
        transaction_committed = verified
        verification_source = result.verification_source or "external_action"
        rollback_outcome = str(result.result_data.get("rollback_status") or "unknown")
        verification = VerificationResult(
            verified=verified,
            source=verification_source,
            reason_code=result.error_code or ("verified" if verified else "not_verified"),
            evidence_refs=(),
        )
        if trajectory_id is not None:
            self._trajectory_records.record_result(trajectory_id, result, verification)

        post = self._context_provider()
        if not isinstance(post, DirectorV2Context):
            raise ValueError("canary post-context provider returned an invalid context")
        next_proposal = self._proposal_provider(post)
        if not isinstance(next_proposal, DirectorV2Proposal):
            raise ValueError("canary next proposal is invalid")
        world_changed = pre.world_snapshot_id != post.world_snapshot_id
        capability_rechecked = (
            "capability" not in post.source_failures
            and post.capability_snapshot_id != "capabilities-unavailable"
        )
        passed = (
            verified and transaction_committed and world_projected
            and world_changed and capability_rechecked
            and next_proposal.proposal_id != proposal.proposal_id
        )
        outcome = "passed" if passed else "failed"
        reason = "closed_loop_verified" if passed else self._failure_reason(
            verified=verified,
            world_projected=world_projected,
            world_changed=world_changed,
            capability_rechecked=capability_rechecked,
            next_changed=next_proposal.proposal_id != proposal.proposal_id,
        )
        self._sequence += 1
        record = ClosedLoopCanaryRecord(
            schema_version=self.config.schema_version,
            canary_id=f"canary-{self._sequence:06d}",
            source_revision=source.revision,
            current_product_version=self.current_product_version,
            target_product_version=self.target_product_version,
            started_at=started,
            completed_at=self._utc_now(),
            action_id=self._label(request.action_id),
            proposal_id=self._label(proposal.proposal_id),
            action_type=self._label(request.action_type),
            capability_id=self._label(request.capability_id),
            pre_snapshot=self._refs(pre),
            post_snapshot=self._refs(post),
            result_status=result.status.value,
            verified=verified,
            verification_source=self._label(verification_source),
            transaction_committed=transaction_committed,
            world_projected=world_projected,
            capability_rechecked=capability_rechecked,
            next_proposal_id=self._label(next_proposal.proposal_id),
            next_action_type=self._label(next_proposal.action_type),
            outcome=outcome,
            reason_code=reason,
            rollback_outcome=self._label(rollback_outcome),
        )
        self._records.append(record)
        self._record_metric(outcome)
        return record

    def snapshot(self) -> dict[str, Any]:
        projection = {
            "enabled": self.enabled,
            "running": self._running,
            "counts": dict(sorted(self._counts.items())),
            "recent": [record.to_dict() for record in reversed(self._records)],
        }
        return json.loads(json.dumps(projection, ensure_ascii=False))

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"closed_loop_canary_{key}_total": value
            for key, value in sorted(self._counts.items())
        }

    def _record_metric(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_closed_loop_canary", None)
        if callable(recorder):
            try:
                recorder(outcome)
            except Exception:
                pass

    def _label(self, value: str) -> str:
        text = " ".join(str(value).split())
        if not text or len(text) > self.config.max_label_chars:
            raise ValueError("canary label is invalid")
        return text

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("canary clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _refs(context: DirectorV2Context) -> TrajectorySnapshotRefs:
        return TrajectorySnapshotRefs(
            context.world_snapshot_id,
            context.self_snapshot_id,
            context.capability_snapshot_id,
        )

    @staticmethod
    def _failure_reason(**gates: bool) -> str:
        return next((name for name, passed in gates.items() if not passed), "unknown")
