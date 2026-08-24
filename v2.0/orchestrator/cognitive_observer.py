"""Pure observation tap from compatibility Director decisions to MCB-3."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.cognition import (
    CognitionConfig,
    CognitiveCompatibilityObservation,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
    CognitiveOpportunity,
    CognitiveOpportunityKind,
)
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction, DirectorDecision


HardStateProvider = Callable[[DirectorInput], CognitiveHardState]


class CognitiveDirectorObserver:
    """Creates deterministic opportunities without awaiting or reading results."""

    def __init__(
        self,
        *,
        config: CognitionConfig,
        scheduler: Any,
        session_id: str = "stream:runtime",
        hard_state_provider: HardStateProvider | None = None,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._session_id = session_id
        self._hard_state_provider = hard_state_provider or self._default_hard_state

    def observe_decision(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> bool:
        trigger = _trigger(decision)
        if trigger is None:
            return False
        kind, material_ref, event_ref = trigger
        return self._offer(
            decision, director_input, decision_id,
            kind=kind, material_ref=material_ref, event_ref=event_ref,
        )

    def observe_verified_outcome(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> bool:
        identity = decision_id or _digest(
            "decision", decision.action.value, decision.reason,
            str(director_input.now),
        )
        return self._offer(
            decision, director_input, decision_id,
            kind=CognitiveOpportunityKind.VERIFIED_OUTCOME,
            material_ref=_digest("verified", identity),
            event_ref=None,
        )

    def preempt_for_live(self) -> None:
        self._scheduler.preempt_for_live()

    def _offer(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
        *,
        kind: CognitiveOpportunityKind,
        material_ref: str,
        event_ref: str | None,
    ) -> bool:
        opened_at = datetime.fromtimestamp(float(director_input.now), timezone.utc)
        compatibility_mode = (
            CognitiveMode.WAIT
            if decision.action is DirectorAction.WAIT else CognitiveMode.SPEAK
        )
        decision_ref = decision_id or _digest(
            "decision", decision.action.value, decision.reason,
            str(director_input.now), material_ref,
        )
        request_id = _digest("context-request", material_ref, opened_at.isoformat())
        opportunity_id = _digest("opportunity", kind.value, material_ref, request_id)
        compatibility = CognitiveCompatibilityObservation(
            config=self._config,
            schema_version=self._config.schema_version,
            decision_ref=decision_ref,
            mode=compatibility_mode,
            action_label=decision.action.value,
            reason_label=decision.reason,
        )
        request = CognitiveContextRequest(
            config=self._config,
            schema_version=self._config.schema_version,
            request_id=request_id,
            session_id=self._session_id,
            requested_at=opened_at,
            trigger_event_ref=event_ref,
            hard_state=self._hard_state_provider(director_input),
        )
        return self._scheduler.offer(CognitiveOpportunity(
            config=self._config,
            schema_version=self._config.schema_version,
            opportunity_id=opportunity_id,
            kind=kind,
            opened_at=opened_at,
            material_change_ref=material_ref,
            context_request=request,
            compatibility=compatibility,
        ))

    def _default_hard_state(self, director_input: DirectorInput) -> CognitiveHardState:
        return CognitiveHardState(
            config=self._config,
            schema_version=self._config.schema_version,
            emergency=False,
            operator_hold=False,
            safety_hold=bool(director_input.safety_hold),
            permission_hold=False,
            transaction_conflict=False,
            critical_state=False,
            source_failure_codes=(),
        )


def _trigger(
    decision: DirectorDecision,
) -> tuple[CognitiveOpportunityKind, str, str | None] | None:
    if decision.refs:
        top = decision.refs[0]
        event_ref = (
            top.msg_id
            if top.msg_id.startswith("agent:chat:")
            else f"agent:chat:{top.msg_id}"
        )
        kind = (
            CognitiveOpportunityKind.DONATION_OR_OPERATOR
            if top.is_super or top.is_owner or top.is_moderator
            else CognitiveOpportunityKind.CHAT_INPUT
        )
        return kind, event_ref, event_ref
    if decision.action in {
        DirectorAction.CONTINUE_THREAD,
        DirectorAction.ASK_FOLLOW_UP,
        DirectorAction.FOLLOW_UP,
        DirectorAction.SHARE_GOAL_PROGRESS,
    }:
        identity = decision.goal_id or decision.proactive_source_id or decision.reason
        return (
            CognitiveOpportunityKind.CONVERSATION_CONTINUATION,
            _digest("continuation", identity, decision.action.value),
            None,
        )
    if decision.action in {DirectorAction.SELF_TALK, DirectorAction.TRANSITION}:
        identity = decision.proactive_source_id or decision.next_segment or decision.reason
        return (
            CognitiveOpportunityKind.PROACTIVE_READY,
            _digest("proactive", identity, decision.action.value),
            None,
        )
    return None


def _digest(prefix: str, *parts: str) -> str:
    encoded = "\n".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"
