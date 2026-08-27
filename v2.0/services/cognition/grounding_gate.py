"""Deterministic fail-closed grounding policy for Cognitive Brain proposals."""
from __future__ import annotations

import hashlib
from typing import Any

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveContext,
    CognitiveGroundingDecision,
    CognitiveGroundingGateService,
    CognitiveGroundingOutcome,
    CognitiveMode,
    CognitiveTurn,
    CognitiveUncertainty,
    cognitive_context_references,
)


_UNCERTAINTY_RANK = {
    CognitiveUncertainty.LOW: 0,
    CognitiveUncertainty.MEDIUM: 1,
    CognitiveUncertainty.HIGH: 2,
    CognitiveUncertainty.UNKNOWN: 3,
}


class CognitiveGroundingGate(CognitiveGroundingGateService):
    """Convert unsupported Brain proposals to typed WAIT without public effects."""

    service_id = "cognitive_grounding_gate"

    def __init__(self, config: CognitionConfig, *, metrics: Any = None) -> None:
        if not isinstance(config, CognitionConfig):
            raise ValueError("config must be CognitionConfig")
        self._config = config
        self._metrics = metrics
        self._running = False
        self._evaluated = 0
        self._failures = 0
        self._outcomes: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            uncertainty_threshold=self._config.grounding_uncertainty_threshold.value,
            evidence_policy=self._config.grounding_evidence_policy,
        )

    def get_metrics(self) -> dict[str, Any]:
        passed = self._outcomes.get(CognitiveGroundingOutcome.PASSED.value, 0)
        return {
            "cognitive_grounding_gate_running": self._running,
            "cognitive_grounding_gate_evaluated_total": self._evaluated,
            "cognitive_grounding_gate_failures_total": self._failures,
            "cognitive_grounding_gate_outcomes": dict(sorted(self._outcomes.items())),
            "cognitive_grounding_gate_pass_rate": (
                passed / self._evaluated if self._evaluated else 0.0
            ),
        }

    def evaluate(
        self, context: CognitiveContext, turn: CognitiveTurn,
    ) -> CognitiveGroundingDecision:
        try:
            if not self._running:
                raise RuntimeError("cognitive grounding gate is not running")
            if not isinstance(context, CognitiveContext) or not isinstance(
                turn, CognitiveTurn,
            ):
                raise TypeError("grounding requires CognitiveContext and CognitiveTurn")
            if turn.context_id != context.context_id:
                raise ValueError("grounding turn context_id is stale or mismatched")

            if turn.mode is CognitiveMode.WAIT:
                return self._decision(
                    context, turn, CognitiveGroundingOutcome.SUPPRESSED_WAIT, turn,
                )
            if _UNCERTAINTY_RANK[turn.uncertainty] > _UNCERTAINTY_RANK[
                self._config.grounding_uncertainty_threshold
            ]:
                return self._suppressed(
                    context, turn, CognitiveGroundingOutcome.SUPPRESSED_UNCERTAINTY,
                )
            if not turn.evidence_refs:
                return self._suppressed(
                    context, turn, CognitiveGroundingOutcome.SUPPRESSED_EMPTY_EVIDENCE,
                )
            current_refs = cognitive_context_references(context)
            if any(ref not in current_refs for ref in turn.evidence_refs):
                return self._suppressed(
                    context, turn, CognitiveGroundingOutcome.SUPPRESSED_UNKNOWN_EVIDENCE,
                )
            return self._decision(
                context, turn, CognitiveGroundingOutcome.PASSED, turn,
            )
        except Exception:
            self._record_failure()
            raise

    def _suppressed(
        self,
        context: CognitiveContext,
        turn: CognitiveTurn,
        outcome: CognitiveGroundingOutcome,
    ) -> CognitiveGroundingDecision:
        digest = hashlib.sha256(
            f"{context.context_id}\n{turn.turn_id}\n{outcome.value}".encode("utf-8")
        ).hexdigest()
        effective = CognitiveTurn(
            config=self._config,
            context=context,
            schema_version=self._config.schema_version,
            turn_id=f"grounded-wait:{digest}",
            context_id=context.context_id,
            mode=CognitiveMode.WAIT,
            attention_target_id=None,
            intent=None,
            speech_text=None,
            action_proposal=None,
            focus_proposal=None,
            memory_proposals=(),
            evidence_refs=(),
            uncertainty=turn.uncertainty,
            reason_codes=("insufficient_evidence",),
        )
        return self._decision(context, turn, outcome, effective)

    def _decision(
        self,
        context: CognitiveContext,
        source: CognitiveTurn,
        outcome: CognitiveGroundingOutcome,
        effective: CognitiveTurn,
    ) -> CognitiveGroundingDecision:
        decision = CognitiveGroundingDecision(
            config=self._config,
            schema_version=self._config.schema_version,
            source_turn_id=source.turn_id,
            context_id=context.context_id,
            source_mode=source.mode,
            source_uncertainty=source.uncertainty,
            outcome=outcome,
            effective_turn=effective,
        )
        self._evaluated += 1
        self._outcomes[outcome.value] = self._outcomes.get(outcome.value, 0) + 1
        _call_metric(self._metrics, "record_cognitive_grounding_decision", outcome.value)
        return decision

    def _record_failure(self) -> None:
        self._failures += 1
        _call_metric(self._metrics, "record_cognitive_grounding_decision", "FAILURE")


def _call_metric(metrics: Any, method: str, *args: Any) -> None:
    recorder = getattr(metrics, method, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass
