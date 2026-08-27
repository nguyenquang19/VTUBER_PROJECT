"""Single live tick owner for compatibility, shadow, and Brain public canary."""
from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveBrainShadowSchedulerService,
    CognitiveCompatibilityObservation,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
    CognitiveOpportunity,
    CognitiveOpportunityKind,
)
from interfaces.turn_kernel import (
    KernelConfig,
    PublicTurnRoute,
    TurnKernelService,
    TurnOpportunity,
    TurnOwner,
    TurnOwnerSelection,
    TurnPreflight,
    TurnRouteOutcome,
    TurnRolloutMode,
)
from interfaces.operations import TurnJournalEvent, TurnJournalStage
from orchestrator.logger import get_logger
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction, DirectorDecision


HardStateProvider = Callable[[DirectorInput], CognitiveHardState]


class TurnKernel(TurnKernelService):
    """Own the only runtime tick, hard preflight, and public owner route."""

    service_id = "turn_kernel"

    def __init__(
        self,
        *,
        config: KernelConfig,
        cognition_config: CognitionConfig,
        compatibility: Any,
        brain_scheduler: CognitiveBrainShadowSchedulerService,
        hard_state_provider: HardStateProvider,
        output_filter: Any = None,
        metrics: Any = None,
        turn_journal: Any = None,
        session_id: str = "stream:runtime",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._cognition_config = cognition_config
        self._compatibility = compatibility
        self._brain_scheduler = brain_scheduler
        self._hard_state_provider = hard_state_provider
        self._output_filter = output_filter
        self._metrics = metrics
        self._turn_journal = turn_journal
        self._session_id = session_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._pending: tuple[
            TurnOpportunity,
            TurnPreflight,
            CognitiveCompatibilityObservation,
        ] | None = None
        self._selections: deque[TurnOwnerSelection] = deque(
            maxlen=config.max_recent_selections,
        )
        self._ticks = 0
        self._selection_counts: dict[str, int] = {}
        self._route_counts: dict[str, int] = {}
        self._public_brain_enabled = False
        self._log = get_logger("turn_kernel")

    async def start(self) -> None:
        if self._running:
            return
        await self._compatibility.start_compatibility()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="turn_kernel")
        self._log.info("turn_kernel_ready", mode=self._config.rollout_mode.value)

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._pending = None
        await self._compatibility.stop_compatibility()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if self._task is None or self._task.done():
            return HealthStatus.degraded(self.service_id, "kernel tick task unavailable")
        return HealthStatus.healthy(
            self.service_id,
            rollout_mode=self._config.rollout_mode.value,
            public_owner=(
                "BRAIN_CANARY" if self._public_brain_enabled
                else TurnOwner.COMPATIBILITY.value
            ),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "turn_kernel_running": self._running,
            "turn_kernel_ticks_total": self._ticks,
            "turn_kernel_recent_selections": len(self._selections),
            "turn_kernel_rollout_mode": self._config.rollout_mode.value,
            "turn_kernel_public_owner": (
                "BRAIN_CANARY" if self._public_brain_enabled
                else TurnOwner.COMPATIBILITY.value
            ),
            "turn_kernel_selection_total": dict(sorted(self._selection_counts.items())),
            "turn_kernel_route_total": dict(sorted(self._route_counts.items())),
        }

    @property
    def public_brain_enabled(self) -> bool:
        return self._public_brain_enabled

    def set_public_brain_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        if enabled:
            if self._config.rollout_mode is not TurnRolloutMode.PUBLIC_BRAIN:
                raise RuntimeError("public Brain requires kernel rollout mode BRAIN")
            snapshot = self._brain_scheduler.snapshot()
            if not snapshot.running or not snapshot.healthy:
                raise RuntimeError("public Brain requires a healthy Brain scheduler")
            if self._output_filter is None:
                raise RuntimeError("public Brain requires the output filter")
        self._public_brain_enabled = enabled

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._config.tick_seconds)
                if self._compatibility.turn_in_progress:
                    continue
                await self.tick_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - defensive live boundary
                self._log.error("turn_kernel_tick_failed", error=type(exc).__name__)

    async def tick_once(self) -> object:
        self._pending = None
        result = await self._compatibility.tick_once()
        self._ticks += 1
        self._pending = None
        return result

    def observe_decision(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> bool:
        """Compatibility observer used by OFF/SHADOW tests and older sinks."""
        prepared = self._prepare_decision(decision, director_input, decision_id)
        if prepared is None:
            return False
        opportunity, preflight, observation, event_ref = prepared
        self._pending = (opportunity, preflight, observation)
        if self._config.rollout_mode is not TurnRolloutMode.OFF:
            try:
                self._brain_scheduler.offer(self._cognitive_opportunity(
                    opportunity, observation,
                ))
            except Exception as exc:
                self._log.warning(
                    "cognitive_live_shadow_offer_failed",
                    error=type(exc).__name__,
                )
        self._finish_route(
            opportunity, preflight, observation, event_ref,
            owner=TurnOwner.COMPATIBILITY,
            route_outcome=(
                "compatibility_off"
                if self._config.rollout_mode is TurnRolloutMode.OFF
                else "compatibility_shadow"
            ),
        )
        return True

    async def route_decision(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> PublicTurnRoute | None:
        """Apply hard preflight, canary policy, grounding, filter, then route."""
        prepared = self._prepare_decision(decision, director_input, decision_id)
        if prepared is None:
            return None
        opportunity, preflight, observation, event_ref = prepared
        self._pending = (opportunity, preflight, observation)
        cognitive = self._cognitive_opportunity(opportunity, observation)

        if not preflight.allowed:
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "compatibility_hard_hold", TurnRouteOutcome.COMPATIBILITY,
                mode=CognitiveMode.WAIT,
            )
        if self._config.rollout_mode is TurnRolloutMode.OFF:
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "compatibility_off", TurnRouteOutcome.COMPATIBILITY,
            )
        if self._config.rollout_mode is TurnRolloutMode.SHADOW:
            self._offer_shadow(cognitive)
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "compatibility_shadow", TurnRouteOutcome.COMPATIBILITY,
            )
        if not self._public_brain_enabled:
            self._offer_shadow(cognitive)
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "compatibility_flag_off", TurnRouteOutcome.COMPATIBILITY,
            )
        if not _is_canary(decision, self._config.brain_canary_roles):
            self._offer_shadow(cognitive)
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "compatibility_outside_canary", TurnRouteOutcome.COMPATIBILITY,
            )

        try:
            grounded = await self._brain_scheduler.resolve_public(cognitive)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "cognitive_public_resolution_failed", error=type(exc).__name__,
            )
            grounded = None
        if grounded is None:
            return self._compatibility_route(
                opportunity, preflight, observation, event_ref,
                "fallback_brain", TurnRouteOutcome.FALLBACK,
            )
        effective = grounded.effective_turn
        if effective.mode is CognitiveMode.SPEAK:
            filter_reason = await self._filter_public_speech(effective.speech_text or "")
            if filter_reason is not None:
                return self._compatibility_route(
                    opportunity, preflight, observation, event_ref,
                    filter_reason, TurnRouteOutcome.FALLBACK,
                )
            route = PublicTurnRoute(
                config=self._config,
                cognition_config=self._cognition_config,
                schema_version=self._config.schema_version,
                opportunity_id=opportunity.opportunity_id,
                owner=TurnOwner.BRAIN,
                outcome=TurnRouteOutcome.BRAIN_SPEAK,
                mode=CognitiveMode.SPEAK,
                speech_text=effective.speech_text,
                source_turn_id=grounded.source_turn_id,
                evidence_refs=effective.evidence_refs,
                reason_code="brain_speak",
            )
            self._finish_route(
                opportunity, preflight, observation, event_ref,
                owner=TurnOwner.BRAIN, route_outcome="brain_speak",
                mode=CognitiveMode.SPEAK,
                source_turn_id=grounded.source_turn_id,
                evidence_refs=effective.evidence_refs,
            )
            return route

        route_reason = (
            "brain_action_suppressed"
            if grounded.source_mode is CognitiveMode.PROPOSE_ACTION
            else "brain_wait"
        )
        route = PublicTurnRoute(
            config=self._config,
            cognition_config=self._cognition_config,
            schema_version=self._config.schema_version,
            opportunity_id=opportunity.opportunity_id,
            owner=TurnOwner.BRAIN,
            outcome=TurnRouteOutcome.BRAIN_WAIT,
            mode=CognitiveMode.WAIT,
            speech_text=None,
            source_turn_id=grounded.source_turn_id,
            evidence_refs=(),
            reason_code=route_reason,
        )
        self._finish_route(
            opportunity, preflight, observation, event_ref,
            owner=TurnOwner.BRAIN, route_outcome=route_reason,
            mode=CognitiveMode.WAIT,
            source_turn_id=grounded.source_turn_id,
        )
        return route

    def _prepare_decision(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> tuple[
        TurnOpportunity, TurnPreflight, CognitiveCompatibilityObservation, str | None,
    ] | None:
        trigger = _trigger(decision)
        if trigger is None:
            self._pending = None
            return None
        kind, material_ref, event_ref = trigger
        opened_at = datetime.fromtimestamp(float(director_input.now), timezone.utc)
        hard_state = self._hard_state_provider(director_input)
        request_id = _digest("context-request", material_ref, opened_at.isoformat())
        opportunity_id = _digest("opportunity", kind.value, material_ref, request_id)
        request = CognitiveContextRequest(
            config=self._cognition_config,
            schema_version=self._cognition_config.schema_version,
            request_id=request_id,
            session_id=self._session_id,
            requested_at=opened_at,
            trigger_event_ref=event_ref,
            hard_state=hard_state,
        )
        opportunity = TurnOpportunity(
            schema_version=self._config.schema_version,
            opportunity_id=opportunity_id,
            opened_at=opened_at,
            kind=kind,
            material_change_ref=material_ref,
            context_request=request,
        )
        reasons = _hard_reasons(hard_state)
        preflight = TurnPreflight(
            schema_version=self._config.schema_version,
            opportunity_id=opportunity_id,
            checked_at=opened_at,
            allowed=not reasons,
            hard_state=hard_state,
            reason_codes=reasons,
        )
        compatibility_mode = (
            CognitiveMode.WAIT
            if decision.action is DirectorAction.WAIT else CognitiveMode.SPEAK
        )
        observation = CognitiveCompatibilityObservation(
            config=self._cognition_config,
            schema_version=self._cognition_config.schema_version,
            decision_ref=decision_id or _digest(
                "decision", decision.action.value, decision.reason,
                str(director_input.now), material_ref,
            ),
            mode=compatibility_mode,
            action_label=decision.action.value,
            reason_label=decision.reason,
        )
        return opportunity, preflight, observation, event_ref

    def _finish_route(
        self,
        opportunity: TurnOpportunity,
        preflight: TurnPreflight,
        observation: CognitiveCompatibilityObservation,
        event_ref: str | None,
        *,
        owner: TurnOwner,
        route_outcome: str,
        mode: CognitiveMode | None = None,
        source_turn_id: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> None:
        selection = TurnOwnerSelection(
            schema_version=self._config.schema_version,
            opportunity_id=opportunity.opportunity_id,
            selected_at=opportunity.opened_at,
            rollout_mode=self._config.rollout_mode,
            owner=owner,
            selection_ref=_digest(
                "selection", opportunity.opportunity_id,
                self._config.rollout_mode.value, owner.value,
            ),
        )
        self._selections.append(selection)
        key = f"{selection.rollout_mode.value}:{selection.owner.value}"
        self._selection_counts[key] = self._selection_counts.get(key, 0) + 1
        self._route_counts[route_outcome] = self._route_counts.get(route_outcome, 0) + 1
        _call_metric(
            self._metrics, "record_turn_kernel_selection",
            selection.rollout_mode.value, selection.owner.value,
            "allowed" if preflight.allowed else "hard_hold",
        )
        _call_metric(self._metrics, "record_turn_kernel_route", route_outcome)
        lineage_id = observation.decision_ref
        if event_ref is not None:
            self._append_journal(TurnJournalEvent(
                schema_version=1,
                lineage_id=lineage_id,
                stage=TurnJournalStage.EVENT_RECEIVED,
                occurred_at=opportunity.opened_at,
                session_id=self._session_id,
                event_id=event_ref,
                opportunity_id=opportunity.opportunity_id,
                decision_id=observation.decision_ref,
                owner=owner.value,
                mode=(mode or observation.mode).value,
                evidence_refs=(event_ref,),
            ))
        self._append_journal(TurnJournalEvent(
            schema_version=1,
            lineage_id=lineage_id,
            stage=TurnJournalStage.OPPORTUNITY_OPENED,
            occurred_at=opportunity.opened_at,
            session_id=self._session_id,
            event_id=event_ref,
            opportunity_id=opportunity.opportunity_id,
            decision_id=observation.decision_ref,
            owner=owner.value,
            mode=(mode or observation.mode).value,
            reason_codes=preflight.reason_codes,
            evidence_refs=(opportunity.material_change_ref,),
        ))
        self._append_journal(TurnJournalEvent(
            schema_version=1,
            lineage_id=lineage_id,
            stage=TurnJournalStage.DECISION_RECORDED,
            occurred_at=opportunity.opened_at,
            session_id=self._session_id,
            event_id=event_ref,
            opportunity_id=opportunity.opportunity_id,
            decision_id=observation.decision_ref,
            owner=owner.value,
            mode=(mode or observation.mode).value,
            terminal_state=(
                "WAIT" if (mode or observation.mode) is CognitiveMode.WAIT else None
            ),
            turn_id=source_turn_id,
            reason_codes=(route_outcome,),
            evidence_refs=evidence_refs,
        ))

    def _cognitive_opportunity(
        self,
        opportunity: TurnOpportunity,
        observation: CognitiveCompatibilityObservation,
    ) -> CognitiveOpportunity:
        return CognitiveOpportunity(
            config=self._cognition_config,
            schema_version=self._cognition_config.schema_version,
            opportunity_id=opportunity.opportunity_id,
            kind=opportunity.kind,
            opened_at=opportunity.opened_at,
            material_change_ref=opportunity.material_change_ref,
            context_request=opportunity.context_request,
            compatibility=observation,
        )

    def _offer_shadow(self, opportunity: CognitiveOpportunity) -> None:
        try:
            self._brain_scheduler.offer(opportunity)
        except Exception as exc:
            self._log.warning(
                "cognitive_live_shadow_offer_failed", error=type(exc).__name__,
            )

    def _compatibility_route(
        self,
        opportunity: TurnOpportunity,
        preflight: TurnPreflight,
        observation: CognitiveCompatibilityObservation,
        event_ref: str | None,
        reason: str,
        outcome: TurnRouteOutcome,
        *,
        mode: CognitiveMode | None = None,
    ) -> PublicTurnRoute:
        self._finish_route(
            opportunity, preflight, observation, event_ref,
            owner=TurnOwner.COMPATIBILITY, route_outcome=reason, mode=mode,
        )
        return PublicTurnRoute(
            config=self._config,
            cognition_config=self._cognition_config,
            schema_version=self._config.schema_version,
            opportunity_id=opportunity.opportunity_id,
            owner=TurnOwner.COMPATIBILITY,
            outcome=outcome,
            mode=mode or observation.mode,
            speech_text=None,
            source_turn_id=None,
            evidence_refs=(),
            reason_code=reason,
        )

    async def _filter_public_speech(self, text: str) -> str | None:
        try:
            verdict = await self._output_filter.check(
                text, {"source": "cognitive_brain_public"},
            )
        except Exception:
            return "fallback_filter_failure"
        if str(getattr(verdict, "reason", "")).startswith("fail-open:"):
            return "fallback_filter_failure"
        if getattr(verdict, "passed", False) is not True:
            return "fallback_filter_reject"
        return None

    def observe_verified_outcome(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> bool:
        """Verified delivery enriches state; it never opens a second Brain request."""
        return self._pending is not None

    def notify_input_activity(self) -> None:
        self._pending = None
        try:
            self._brain_scheduler.preempt_for_live()
        except Exception:
            pass

    def preempt_for_live(self) -> None:
        self.notify_input_activity()

    def _append_journal(self, event: TurnJournalEvent) -> None:
        if self._turn_journal is None:
            return
        try:
            self._turn_journal.append(event)
        except Exception as exc:
            self._log.warning(
                "turn_journal_observation_failed",
                stage=event.stage.value,
                error=type(exc).__name__,
            )

    def recent_selections(
        self, limit: int | None = None,
    ) -> tuple[TurnOwnerSelection, ...]:
        values = tuple(self._selections)
        if limit is None:
            return values
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        return values[-limit:] if limit else ()


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


def _hard_reasons(state: CognitiveHardState) -> tuple[str, ...]:
    reasons: list[str] = []
    for name in (
        "emergency", "operator_hold", "safety_hold", "permission_hold",
        "transaction_conflict", "critical_state",
    ):
        if getattr(state, name):
            reasons.append(name)
    if state.source_failure_codes:
        reasons.append("source_failure")
    return tuple(reasons)


def _is_canary(decision: DirectorDecision, roles: tuple[str, ...]) -> bool:
    if not decision.refs:
        return False
    primary = decision.refs[0]
    return any((
        "owner" in roles and bool(primary.is_owner),
        "moderator" in roles and bool(primary.is_moderator),
    ))


def _digest(prefix: str, *parts: str) -> str:
    value = "\n".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(value).hexdigest()}"


def _call_metric(metrics: Any, method: str, *args: Any) -> None:
    recorder = getattr(metrics, method, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass
