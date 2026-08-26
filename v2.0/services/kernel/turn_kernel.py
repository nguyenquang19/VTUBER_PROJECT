"""Single live tick owner for S4 compatibility + Brain shadow scheduling."""
from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.cognition import (
    CognitionConfig,
    CognitiveCompatibilityObservation,
    CognitiveContextRequest,
    CognitiveHardState,
    CognitiveMode,
    CognitiveOpportunity,
    CognitiveOpportunityKind,
)
from interfaces.turn_kernel import (
    KernelConfig,
    TurnKernelService,
    TurnOpportunity,
    TurnOwner,
    TurnOwnerSelection,
    TurnPreflight,
    TurnRolloutMode,
)
from interfaces.operations import TurnJournalEvent, TurnJournalStage
from orchestrator.logger import get_logger
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction, DirectorDecision


HardStateProvider = Callable[[DirectorInput], CognitiveHardState]


class TurnKernel(TurnKernelService):
    """Own the only runtime tick; S4 always routes public work to compatibility."""

    service_id = "turn_kernel"

    def __init__(
        self,
        *,
        config: KernelConfig,
        cognition_config: CognitionConfig,
        compatibility: Any,
        brain_scheduler: Any,
        hard_state_provider: HardStateProvider,
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
            public_owner=TurnOwner.COMPATIBILITY.value,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "turn_kernel_running": self._running,
            "turn_kernel_ticks_total": self._ticks,
            "turn_kernel_recent_selections": len(self._selections),
            "turn_kernel_rollout_mode": self._config.rollout_mode.value,
            "turn_kernel_public_owner": TurnOwner.COMPATIBILITY.value,
            "turn_kernel_selection_total": dict(sorted(self._selection_counts.items())),
        }

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
        pending = self._pending
        self._pending = None
        if pending is not None and self._config.rollout_mode is TurnRolloutMode.SHADOW:
            opportunity, _preflight, observation = pending
            self._brain_scheduler.offer(CognitiveOpportunity(
                config=self._cognition_config,
                schema_version=self._cognition_config.schema_version,
                opportunity_id=opportunity.opportunity_id,
                kind=opportunity.kind,
                opened_at=opportunity.opened_at,
                material_change_ref=opportunity.material_change_ref,
                context_request=opportunity.context_request,
                compatibility=observation,
            ))
        return result

    def observe_decision(
        self,
        decision: DirectorDecision,
        director_input: DirectorInput,
        decision_id: str | None,
    ) -> bool:
        """Capture one opportunity; Brain work is offered only after public execution."""
        trigger = _trigger(decision)
        if trigger is None:
            self._pending = None
            return False
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
        selection = TurnOwnerSelection(
            schema_version=self._config.schema_version,
            opportunity_id=opportunity_id,
            selected_at=opened_at,
            rollout_mode=self._config.rollout_mode,
            owner=TurnOwner.COMPATIBILITY,
            selection_ref=_digest(
                "selection", opportunity_id, self._config.rollout_mode.value,
                TurnOwner.COMPATIBILITY.value,
            ),
        )
        self._selections.append(selection)
        key = f"{selection.rollout_mode.value}:{selection.owner.value}"
        self._selection_counts[key] = self._selection_counts.get(key, 0) + 1
        _call_metric(
            self._metrics, "record_turn_kernel_selection",
            selection.rollout_mode.value, selection.owner.value,
            "allowed" if preflight.allowed else "hard_hold",
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
        lineage_id = decision_id or opportunity_id
        if event_ref is not None:
            self._append_journal(TurnJournalEvent(
                schema_version=1,
                lineage_id=lineage_id,
                stage=TurnJournalStage.EVENT_RECEIVED,
                occurred_at=opened_at,
                session_id=self._session_id,
                event_id=event_ref,
                opportunity_id=opportunity_id,
                decision_id=decision_id,
                owner=TurnOwner.COMPATIBILITY.value,
                mode=compatibility_mode.value,
                evidence_refs=(event_ref,),
            ))
        self._append_journal(TurnJournalEvent(
            schema_version=1,
            lineage_id=lineage_id,
            stage=TurnJournalStage.OPPORTUNITY_OPENED,
            occurred_at=opened_at,
            session_id=self._session_id,
            event_id=event_ref,
            opportunity_id=opportunity_id,
            decision_id=decision_id,
            owner=TurnOwner.COMPATIBILITY.value,
            mode=compatibility_mode.value,
            reason_codes=reasons,
            evidence_refs=((material_ref,) if material_ref else ()),
        ))
        self._append_journal(TurnJournalEvent(
            schema_version=1,
            lineage_id=lineage_id,
            stage=TurnJournalStage.DECISION_RECORDED,
            occurred_at=opened_at,
            session_id=self._session_id,
            event_id=event_ref,
            opportunity_id=opportunity_id,
            decision_id=decision_id,
            owner=TurnOwner.COMPATIBILITY.value,
            mode=compatibility_mode.value,
            terminal_state=("WAIT" if compatibility_mode is CognitiveMode.WAIT else None),
            reason_codes=((decision.reason,) if decision.reason else ()),
        ))
        self._pending = (opportunity, preflight, observation)
        return True

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
