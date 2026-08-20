"""DirectorLoop — turn driver duy nhất (C0.4; xem docs/MAI_V2_SYSTEM_SPEC.md).

Bỏ FIFO: ChatRouter chỉ bơm chat vào SaliencePool + ChatPulse (intake mode).
DirectorLoop là VÒNG DUY NHẤT sinh turn — tick định kỳ, hỏi Director nên làm gì,
rồi thực thi qua turn_lock (không 2 turn LLM cùng lúc, TTS không overlap).

    tick → evict_stale → decide(urge_ready) → execute action → mark_spoke

Execute:
  READ_CHAT/ACK_DONATION — dựng user_text từ refs → run_turn → speak → pool.remove
  SELF_TALK/FOLLOW_UP     — autonomy.force_generate → run_ambient_turn → commit → speak
  TRANSITION              — Mai thông báo chuyển segment → speak → advance_segment
  WAIT                    — bỏ qua tick này

clock inject (test tất định). Mọi lỗi execute fail-safe: log + tiếp tick (N7).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from interfaces.action_execution import VerificationResult
from interfaces.animation import MoodState
from interfaces.action_execution import ActionRequest, ActionResult
from interfaces.decision_record import DecisionCandidateSummary
from interfaces.director_v2 import DirectorV2Proposal, DirectorV2TakeoverSelection
from interfaces.self_talk import SelfTalkContext, SelfTalkStage
from orchestrator.logger import get_logger
from services.autonomy.material_provider import RuntimeContext
from services.autonomy.dedup import DedupBuffer
from services.director.chat_pulse import PulseState
from services.director.director import Director, DirectorAction, DirectorDecision, ReadMode
from services.director.v2_primary import (
    DirectorV2DecisionMaterializer,
    DirectorV2MaterializationError,
)
from services.director.action_context import ActionContextBuilder
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.delivery_boundary import DirectorDeliveryBoundary
from services.director.action_prompts import (
    history_text_for as _history_text_for,
    join_directives as _join_directives,
    proactive_thread_directive as _proactive_thread_directive,
    read_user_text as _read_user_text,
    room_reaction_prompt as _room_reaction_prompt,
    room_reaction_correction_prompt as _room_reaction_correction_prompt,
    speech_dedup_correction_prompt as _speech_dedup_correction_prompt,
    speech_style_constraint_prompt as _speech_style_constraint_prompt,
    speech_style_correction_prompt as _speech_style_correction_prompt,
    self_talk_correction_prompt as _self_talk_correction_prompt,
    stage_direction_for as _stage_direction_for,
    timestamp as _timestamp,
)
from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)
from services.director.speech_style import SpeechStyleAssessment, SpeechStyleGuard

SpeakFn = Callable[[str, str], Awaitable[Any]]


class DirectorLoop:
    def __init__(
        self,
        director: Director,
        pool: Any,
        pulse: Any,
        runner: Any,
        emotion: Any = None,
        autonomy: Any = None,
        speak: SpeakFn | None = None,
        turn_lock: asyncio.Lock | None = None,
        tick_seconds: float = 1.0,
        max_refs: int = 3,
        clock: Callable[[], float] | None = None,
        runtime_ctx_fn: Callable[[], RuntimeContext] | None = None,
        agent_state: Any = None,
        goal_manager: Any = None,
        metrics: Any = None,
        goal_arbitration_enabled: bool = True,
        safety_hold_fn: Callable[[], bool] | None = None,
        action_context_builder: ActionContextBuilder | None = None,
        behavior_library: Any = None,
        repair_policy: Any = None,
        transaction_manager: Any = None,
        decision_records: Any = None,
        trajectory_records: Any = None,
        self_talk_planner: Any = None,
        thread_manager: Any = None,
        animation: Any = None,
        embodiment_policy: Any = None,
        action_adapter_boundary: Any = None,
        room_reaction_recent_window: int = 16,
        room_reaction_similarity_threshold: float = 0.72,
        room_reaction_max_regenerations: int = 1,
        room_reaction_retry_defer_seconds: float = 30.0,
        speech_dedup_recent_window: int = 32,
        speech_dedup_similarity_threshold: float = 0.72,
        speech_dedup_max_regenerations: int = 1,
        speech_style_recent_window: int = 12,
        speech_style_formula_openers: tuple[str, ...] = (
            "mà", "trời ơi", "ủa", "ơ kìa",
        ),
        speech_style_max_formula_openers: int = 2,
        speech_style_max_same_opener: int = 1,
        speech_style_max_questions: int = 2,
        speech_style_question_endings: tuple[str, ...] = (
            "nhỉ", "hả", "à", "ư", "không", "chưa", "sao", "gì", "nào",
        ),
        speech_style_max_sentences: int = 2,
        speech_style_max_words: int = 65,
        speech_style_max_regenerations: int = 1,
    ) -> None:
        self._director = director
        self._pool = pool
        self._pulse = pulse
        self._runner = runner
        self._emotion = emotion
        self._autonomy = autonomy
        self._speak = speak
        self._turn_lock = turn_lock or asyncio.Lock()
        self._tick_s = float(tick_seconds)
        self._max_refs = int(max_refs)
        self._clock = clock or time.time
        self._runtime_ctx_fn = runtime_ctx_fn
        self._agent_state = agent_state
        self._goal_manager = goal_manager
        self._metrics = metrics
        self._goal_arbitration_enabled = bool(goal_arbitration_enabled)
        self._safety_hold_fn = safety_hold_fn
        self._action_context_builder = action_context_builder or ActionContextBuilder()
        self._behavior_library = behavior_library
        self._repair_policy = repair_policy
        self._transactions = transaction_manager
        self._decision_records = decision_records
        self._trajectory_records = trajectory_records
        self._trajectory_by_decision: dict[str, str] = {}
        self._trajectory_requested_at: dict[str, datetime] = {}
        self._self_talk_planner = self_talk_planner
        self._thread_manager = thread_manager
        self._animation = animation
        self._embodiment_policy = embodiment_policy
        self._action_adapter_boundary = action_adapter_boundary
        self._director_v2_shadow = None
        self._director_v2_takeover = None
        self._director_v2_materializer: DirectorV2DecisionMaterializer | None = None
        self._director_v2_primary_selected_total = 0
        self._director_v2_primary_fallback_total = 0
        self._director_v2_hard_preemption_total = 0
        self._room_reaction_dedup = DedupBuffer(
            window=room_reaction_recent_window,
            threshold=room_reaction_similarity_threshold,
        )
        self._room_reaction_max_regenerations = max(
            0, int(room_reaction_max_regenerations),
        )
        self._room_reaction_retry_defer_seconds = max(
            0.0, float(room_reaction_retry_defer_seconds),
        )
        self._speech_dedup = DedupBuffer(
            window=speech_dedup_recent_window,
            threshold=speech_dedup_similarity_threshold,
        )
        self._speech_dedup_max_regenerations = max(
            0, int(speech_dedup_max_regenerations),
        )
        self._speech_style = SpeechStyleGuard(
            recent_window=speech_style_recent_window,
            formula_openers=speech_style_formula_openers,
            max_formula_openers=speech_style_max_formula_openers,
            max_same_opener=speech_style_max_same_opener,
            max_questions=speech_style_max_questions,
            question_endings=speech_style_question_endings,
            max_sentences=speech_style_max_sentences,
            max_words=speech_style_max_words,
        )
        self._speech_style_max_regenerations = max(
            0, int(speech_style_max_regenerations),
        )

        self._task: asyncio.Task | None = None
        self._running = False
        self._log = get_logger("director_loop")
        self._turns_read = 0
        self._turns_self = 0
        self._transitions = 0
        self._chat_suppressed_total = 0
        self._last_pulse_state: PulseState | None = None  # edge debounce
        self._pulse_mood_pushes = 0
        self._filter_context_quarantined_total = 0
        self._execute_failed_total = 0
        self._room_reaction_generated_total = 0
        self._room_reaction_duplicate_total = 0
        self._room_reaction_regenerated_total = 0
        self._room_reaction_suppressed_total = 0
        self._room_reaction_cooldown_blocked_total = 0
        self._speech_dedup_generated_total = 0
        self._speech_dedup_duplicate_total = 0
        self._speech_dedup_regenerated_total = 0
        self._speech_dedup_suppressed_total = 0
        self._speech_dedup_quarantined_total = 0
        self._speech_style_violation_total = 0
        self._speech_style_regenerated_total = 0
        self._speech_style_exhausted_total = 0
        self._speech_style_clamped_total = 0
        self._thread_focus_total = 0
        self._thread_boundary_clear_total = 0
        self._thread_forced_park_total = 0

    def configure_director_v2_takeover(self, shadow: Any, selector: Any) -> None:
        """Attach the strict V2 ownership gate; DirectorLoop remains the driver."""
        self._director_v2_shadow = shadow
        self._director_v2_takeover = selector
        self._director_v2_materializer = DirectorV2DecisionMaterializer(self._director)

    def _open_director_v2_trajectory(
        self, proposal: DirectorV2Proposal | None,
    ) -> str | None:
        if proposal is None or self._trajectory_records is None:
            return None
        try:
            context = self._director_v2_shadow.trajectory_context(
                proposal.proposal_id,
            )
            if context is not None:
                return self._trajectory_records.begin(context, proposal)
        except Exception as exc:
            self._log.warning("trajectory_begin_failed", error=str(exc))
        return None

    def _apply_director_v2_takeover(
        self, decision: DirectorDecision, director_input: DirectorInput,
    ) -> DirectorDecision:
        if self._director_v2_shadow is None or self._director_v2_takeover is None:
            return decision
        evidence_ids = [ref.msg_id for ref in director_input.chat_candidates]
        if director_input.goals.active is not None:
            evidence_ids.append(director_input.goals.active.goal_id)
        evidence_ids.extend(thread.thread_id for thread in director_input.agent_state.open_threads)
        try:
            proposal = self._director_v2_shadow.propose_current()
        except Exception:
            proposal = None
        trajectory_id = self._open_director_v2_trajectory(proposal)
        try:
            selection = self._director_v2_takeover.evaluate(
                legacy_action=decision.action.value,
                proposal=proposal,
                evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            )
        except Exception:
            self._mark_trajectory_selection(trajectory_id, "legacy")
            return decision
        if (
            not isinstance(selection, DirectorV2TakeoverSelection)
            or selection.accepted is not True
            or selection.decision_owner != "director_v2"
            or proposal is None
            or selection.proposal_id != proposal.proposal_id
            or selection.action_type != decision.action.value.upper()
        ):
            self._mark_trajectory_selection(trajectory_id, "legacy")
            return decision
        self._mark_trajectory_selection(trajectory_id, "director_v2")
        return replace(
            decision,
            decision_owner="director_v2",
            director_v2_proposal_id=selection.proposal_id,
        )

    def _primary_takeover_active(self) -> bool:
        return bool(
            self._director_v2_shadow is not None
            and self._director_v2_takeover is not None
            and self._director_v2_materializer is not None
            and getattr(self._director_v2_takeover, "enabled", False) is True
            and getattr(self._director_v2_takeover, "ownership_mode", None) == "primary"
        )

    def _select_director_decision(
        self, director_input: DirectorInput,
    ) -> DirectorDecision:
        if not self._primary_takeover_active():
            compatibility = self._director.decide(director_input)
            return self._apply_director_v2_takeover(
                compatibility, director_input,
            )

        preemptive = self._director.hard_preemptive_decision(director_input)
        if preemptive is not None:
            self._director_v2_hard_preemption_total += 1
            return preemptive

        evidence_ids = [ref.msg_id for ref in director_input.chat_candidates]
        if director_input.goals.active is not None:
            evidence_ids.append(director_input.goals.active.goal_id)
        evidence_ids.extend(
            thread.thread_id for thread in director_input.agent_state.open_threads
        )
        try:
            proposal = self._director_v2_shadow.propose_current()
        except Exception:
            proposal = None
        trajectory_id = self._open_director_v2_trajectory(
            proposal if isinstance(proposal, DirectorV2Proposal) else None,
        )
        try:
            selection = self._director_v2_takeover.evaluate(
                legacy_action=None,
                proposal=proposal,
                evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            )
        except Exception:
            selection = None
        if (
            isinstance(selection, DirectorV2TakeoverSelection)
            and selection.accepted is False
            and selection.reason_code == "hard_hold"
        ):
            self._mark_trajectory_selection(trajectory_id, "legacy")
            self._director_v2_hard_preemption_total += 1
            return DirectorDecision(
                DirectorAction.WAIT,
                self._director.current_segment().name,
                "director_v2_hard_hold",
            )
        if (
            isinstance(selection, DirectorV2TakeoverSelection)
            and selection.accepted is True
            and selection.decision_owner == "director_v2"
            and isinstance(proposal, DirectorV2Proposal)
            and selection.proposal_id == proposal.proposal_id
            and selection.action_type == proposal.action_type
        ):
            try:
                decision = self._director_v2_materializer.materialize(
                    proposal, director_input,
                )
            except DirectorV2MaterializationError:
                decision = None
            except Exception:
                decision = None
            if decision is not None:
                self._mark_trajectory_selection(trajectory_id, "director_v2")
                self._director_v2_primary_selected_total += 1
                return decision
        self._mark_trajectory_selection(trajectory_id, "legacy")
        self._director_v2_primary_fallback_total += 1
        return self._director.decide(director_input)
    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._transactions is not None:
            await self._transactions.start()
        if self._decision_records is not None:
            await self._decision_records.start()
        if self._self_talk_planner is not None:
            await self._self_talk_planner.start()
        self._director.start(self._clock())
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="director_loop")
        self._log.info("director_loop_ready")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._transactions is not None:
            await self._transactions.stop()
        if self._decision_records is not None:
            await self._decision_records.stop()
        if self._self_talk_planner is not None:
            await self._self_talk_planner.stop()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._tick_s)
                if self._turn_lock.locked():
                    continue   # user/turn khác đang chạy
                await self.tick_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover — defensive
                self._log.error("director_tick_failed", error=str(e))

    # ---------- one tick (public để test) ----------

    async def tick_once(self) -> DirectorAction:
        now = self._clock()
        self._pool.evict_stale(now)
        # Refresh baseline tempo each tick so acceleration cannot stick at 1.0.
        try:
            self._pulse.update_baseline(now)
        except Exception:
            pass
        # ChatPulse nudges mood only on a transition into hype/lively (edge,
        # debounce — không spam mỗi tick).
        await self._maybe_push_pulse_mood(now)
        urge_ready = False
        if self._autonomy is not None:
            try:
                urge_ready = self._autonomy.urge.should_speak_now()
            except Exception:
                pass
        director_input = self._build_director_input(now, urge_ready)
        dec = self._select_director_decision(director_input)
        expected_intention_id = self._decision_intention_id(dec, director_input)
        self._record_director_metric(dec)
        decision_id = self._record_decision(dec, director_input, now)

        if dec.action == DirectorAction.WAIT:
            self._record_trajectory_no_action(decision_id, dec.reason)
            self._record_director_action(dec, now)
            return dec.action

        self._record_trajectory_action(decision_id, dec, director_input, now)

        async with self._turn_lock:
            transaction_id = None
            try:
                if self._transactions is not None and self._transactions.enabled:
                    reservation = self._transactions.reserve(
                        dec.action.value, self._idempotency_key(dec, now),
                    )
                    transaction_id = reservation.transaction.transaction_id
                    self._update_decision_transaction(
                        decision_id, reservation.transaction,
                        delivery_state="not_started", outcome="reserved",
                    )
                    if not reservation.created:
                        self._update_decision_transaction(
                            decision_id, reservation.transaction,
                            delivery_state="delivered", outcome="duplicate_committed",
                        )
                        self._record_director_action(dec, now)
                        return dec.action
                committed = await self._execute(
                    dec, now, director_input, transaction_id=transaction_id,
                )
                if transaction_id is not None:
                    if committed:
                        transaction = self._transactions.commit(transaction_id)
                        self._update_decision_transaction(
                            decision_id, transaction,
                            delivery_state="delivered", outcome="committed",
                        )
                    else:
                        transaction = self._transactions.release(
                            transaction_id, "not_delivered",
                        )
                        self._update_decision_transaction(
                            decision_id, transaction,
                            delivery_state="failed", outcome="released",
                        )
                        self._record_goal_action_outcome(
                            dec,
                            expected_intention_id,
                            transaction_id,
                            outcome="failed",
                            reason="not_delivered",
                        )
                else:
                    self._update_decision_outcome(
                        decision_id,
                        delivery_state="delivered" if committed else "failed",
                        outcome="completed" if committed else "not_delivered",
                    )
                    if not committed:
                        self._record_goal_action_outcome(
                            dec,
                            expected_intention_id,
                            decision_id or f"decision:{dec.action.value}:{now}",
                            outcome="failed",
                            reason="not_delivered",
                        )
                self._record_director_action(dec, now)
            except asyncio.CancelledError:
                if transaction_id is not None:
                    try:
                        transaction = self._transactions.release(
                            transaction_id, "cancelled",
                        )
                        self._update_decision_transaction(
                            decision_id, transaction,
                            delivery_state="cancelled", outcome="released",
                        )
                        self._record_goal_action_outcome(
                            dec,
                            expected_intention_id,
                            transaction_id,
                            outcome="cancelled",
                            reason="execution_cancelled",
                        )
                    except Exception:
                        pass
                else:
                    self._update_decision_outcome(
                        decision_id,
                        delivery_state="cancelled",
                        outcome="execution_cancelled",
                    )
                    self._record_goal_action_outcome(
                        dec,
                        expected_intention_id,
                        decision_id or f"decision:{dec.action.value}:{now}",
                        outcome="cancelled",
                        reason="execution_cancelled",
                    )
                raise
            except Exception as e:
                self._execute_failed_total += 1
                if transaction_id is not None:
                    try:
                        transaction = self._transactions.release(transaction_id, str(e))
                        self._update_decision_transaction(
                            decision_id, transaction,
                            delivery_state="failed", outcome="released",
                        )
                        self._record_goal_action_outcome(
                            dec,
                            expected_intention_id,
                            transaction_id,
                            outcome="failed",
                            reason="execution_failed",
                        )
                    except Exception:
                        pass
                else:
                    self._update_decision_outcome(
                        decision_id,
                        delivery_state="failed",
                        outcome="execution_failed",
                    )
                    self._record_goal_action_outcome(
                        dec,
                        expected_intention_id,
                        decision_id or f"decision:{dec.action.value}:{now}",
                        outcome="failed",
                        reason="execution_failed",
                    )
                self._log.warning("director_execute_failed",
                                  action=dec.action.value, error=str(e))
        return dec.action

    def preview_decision(
        self, now: float | None = None, *, urge_ready: bool = False,
    ) -> Any:
        """Build the same bounded input as a tick without recording or executing."""
        current = self._clock() if now is None else float(now)
        return self._director.decide(
            self._build_director_input(current, bool(urge_ready)),
        )

    def _build_director_input(self, now: float, urge_ready: bool) -> DirectorInput:
        state = AgentStateSnapshot()
        goals = GoalSnapshot()
        try:
            if self._goal_arbitration_enabled and self._agent_state is not None:
                state = self._agent_state.snapshot()
        except Exception:
            pass
        try:
            if self._goal_arbitration_enabled and self._goal_manager is not None:
                self._goal_manager.reconcile_threads({
                    thread.thread_id for thread in state.open_threads
                })
                goals = self._goal_manager.snapshot()
        except Exception:
            pass
        refs = tuple(
            DirectorChatRef(
                msg_id=item.msg_id,
                text=item.text,
                kind=item.kind,
                score=self._pool.current_score(item, now),
                created_at=item.created_at,
                viewer_id=item.viewer_id,
                viewer_name=item.viewer_name,
                amount_vnd=item.amount_vnd,
                is_super=item.is_super,
                cluster_count=item.cluster_count,
            )
            for item in self._pool.top_cluster(now, self._max_refs)
        )
        pulse_state = getattr(self._pulse.state(now), "value", "normal")
        safety_hold = False
        if self._goal_arbitration_enabled and self._safety_hold_fn is not None:
            try:
                safety_hold = bool(self._safety_hold_fn())
            except Exception:
                safety_hold = True
        self_talk_ready, self_talk_wait_reason = self._self_talk_readiness(now)
        return DirectorInput(
            now=now,
            agent_state=state,
            goals=goals,
            chat_candidates=refs,
            pool_size=self._pool.size(),
            pulse_state=pulse_state,
            urge_ready=urge_ready,
            safety_hold=safety_hold,
            mood=self._current_mood(),
            tone_flags=self._tone_flags(),
            self_talk_ready=self_talk_ready,
            self_talk_wait_reason=self_talk_wait_reason,
        )

    @staticmethod
    def _decision_intention_id(dec: Any, value: DirectorInput) -> str | None:
        goal_id = getattr(dec, "goal_id", None)
        intention = value.goals.current_intention
        if (
            not isinstance(goal_id, str)
            or intention is None
            or intention.goal_id != goal_id
        ):
            return None
        return intention.intention_id

    def _current_intention_id(self, goal_id: str | None) -> str | None:
        if not isinstance(goal_id, str) or not goal_id or self._goal_manager is None:
            return None
        try:
            snapshot = self._goal_manager.snapshot()
            intention = snapshot.current_intention
            if (
                snapshot.active is None
                or snapshot.active.goal_id != goal_id
                or intention is None
                or intention.goal_id != goal_id
            ):
                return None
            return intention.intention_id
        except Exception:
            return None

    def _record_goal_action_outcome(
        self,
        dec: Any,
        intention_id: str | None,
        outcome_id: str,
        *,
        outcome: str,
        reason: str,
    ) -> None:
        goal_id = getattr(dec, "goal_id", None)
        if (
            self._goal_manager is None
            or not isinstance(goal_id, str)
            or not goal_id
            or not isinstance(intention_id, str)
            or not intention_id
        ):
            return
        try:
            self._goal_manager.record_action_outcome(
                goal_id,
                intention_id,
                str(outcome_id),
                outcome=outcome,
                reason=reason,
            )
        except Exception as exc:
            self._log.warning(
                "director_goal_outcome_failed",
                goal_id=goal_id,
                intention_id=intention_id,
                outcome=outcome,
                error=str(exc),
            )

    def _self_talk_readiness(self, now: float) -> tuple[bool, str]:
        if self._self_talk_planner is None or not self._self_talk_planner.enabled:
            return True, "legacy"
        try:
            value = self._self_talk_planner.readiness(now)
            return bool(value.ready), str(value.reason)
        except Exception:
            return False, "thought_readiness_failed"

    def set_goal_arbitration_enabled(self, enabled: bool) -> None:
        self._goal_arbitration_enabled = bool(enabled)

    def set_runtime_context_provider(
        self, provider: Callable[[], RuntimeContext] | None,
    ) -> None:
        self._runtime_ctx_fn = provider

    def on_chat_activity(self, now: float | None = None) -> None:
        """Real chat interrupts pending speech and temporarily suspends an arc."""
        self._director.clear_self_talk_defer()
        if self._self_talk_planner is not None:
            self._self_talk_planner.on_chat(self._clock() if now is None else now)

    def _tone_flags(self) -> tuple[str, ...]:
        if self._emotion is None:
            return ()
        try:
            return tuple(sorted(self._emotion.active_tone_flags()))
        except Exception:
            return ()

    @property
    def goal_arbitration_enabled(self) -> bool:
        return self._goal_arbitration_enabled

    async def _maybe_push_pulse_mood(self, now: float) -> None:
        """Emit one EmotionEvent on an edge into HYPE_SPAM or LIVELY.
        nudge mood (chat sôi → vui/bồn_chồn). Debounce theo state cũ."""
        if self._emotion is None:
            return
        try:
            state = self._pulse.state(now)
        except Exception:
            return
        if state == self._last_pulse_state:
            return  # không đổi → không đẩy lại
        self._last_pulse_state = state
        cat = {PulseState.HYPE_SPAM: "chat_hype", PulseState.LIVELY: "chat_lively"}.get(state)
        if cat is None:
            return
        try:
            from services.emotion.classifier import EmotionEvent, EventKind
            await self._emotion.handle_event(
                EmotionEvent(kind=EventKind.SYSTEM, meta={"platform_type": cat})
            )
            self._pulse_mood_pushes += 1
        except Exception as e:
            self._log.warning("pulse_mood_push_failed", error=str(e))

    # ---------- execute ----------

    async def _execute(
        self, dec, now: float, director_input: DirectorInput,
        *, transaction_id: str | None = None,
    ) -> bool:
        if dec.action in (DirectorAction.READ_CHAT, DirectorAction.ACK_DONATION):
            return await self._exec_read(dec, now, transaction_id=transaction_id)
        elif dec.action in (DirectorAction.SELF_TALK, DirectorAction.FOLLOW_UP):
            return await self._exec_self_talk(dec, now, transaction_id=transaction_id)
        elif dec.action == DirectorAction.TRANSITION:
            return await self._exec_transition(dec, now, transaction_id=transaction_id)
        elif dec.action in (
            DirectorAction.CONTINUE_THREAD,
            DirectorAction.ASK_FOLLOW_UP,
            DirectorAction.SHARE_GOAL_PROGRESS,
        ):
            return await self._exec_goal_action(
                dec, now, director_input, transaction_id=transaction_id,
            )
        return False

    async def _exec_goal_action(
        self, dec: Any, now: float, director_input: DirectorInput,
        *, transaction_id: str | None = None,
    ) -> bool:
        context = self._action_context_builder.render(dec, director_input)
        intention_id = self._decision_intention_id(dec, director_input)
        thread = None
        if dec.goal_id and director_input.goals.active is not None:
            parent_id = director_input.goals.active.parent_thread_id
            thread = next(
                (item for item in director_input.agent_state.open_threads
                 if item.thread_id == parent_id),
                None,
            )
        behavior = self._behavior_directive(dec)
        if behavior:
            context = f"{context}\n{behavior}"
        question_budget_exempt = bool(
            thread is not None
            and thread.next_move is not None
            and thread.next_move.value == "invite"
        )
        context = _join_directives(
            context,
            self._speech_style_directive(
                question_budget_exempt=question_budget_exempt,
            ),
        )
        req_id = f"goal_{uuid.uuid4().hex[:8]}"
        parsed = await self._run_directed_deferred(req_id, context)
        if dec.action is DirectorAction.CONTINUE_THREAD:
            self._speech_dedup_generated_total += 1
            if self._speech_candidate_is_duplicate(parsed):
                self._speech_dedup_duplicate_total += 1
                self._finalize_runner_delivery(req_id, False)
                replaced = False
                for attempt in range(self._speech_dedup_max_regenerations):
                    self._speech_dedup_regenerated_total += 1
                    retry_id = f"{req_id}_r{attempt + 1}"
                    retry_context = _speech_dedup_correction_prompt(
                        context,
                        str(getattr(parsed, "text", "")),
                        tuple(self._speech_dedup.recent()),
                    )
                    parsed = await self._run_directed_deferred(retry_id, retry_context)
                    self._speech_dedup_generated_total += 1
                    if not self._speech_candidate_is_duplicate(parsed):
                        req_id = retry_id
                        replaced = True
                        break
                    self._speech_dedup_duplicate_total += 1
                    self._finalize_runner_delivery(retry_id, False)
                if not replaced:
                    self._speech_dedup_suppressed_total += 1
                    if thread is not None:
                        return await self._exec_forced_thread_park(
                            dec,
                            now,
                            thread,
                            intention_id=intention_id,
                            transaction_id=transaction_id,
                        )
                    self._quarantine_repetition_context(
                        refs=[], thread_id=None, goal_id=dec.goal_id,
                    )
                    return False
            req_id, parsed = await self._repair_speech_style(
                req_id,
                parsed,
                context,
                lambda retry_id, retry_context: self._run_directed_deferred(
                    retry_id, retry_context,
                ),
                question_budget_exempt=question_budget_exempt,
            )
            if self._speech_candidate_is_duplicate(parsed):
                self._speech_dedup_duplicate_total += 1
                self._speech_dedup_suppressed_total += 1
                self._finalize_runner_delivery(req_id, False)
                if thread is not None:
                    return await self._exec_forced_thread_park(
                        dec,
                        now,
                        thread,
                        intention_id=intention_id,
                        transaction_id=transaction_id,
                    )
                self._quarantine_repetition_context(
                    refs=[], thread_id=None, goal_id=dec.goal_id,
                )
                return False
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, [], goal_id=dec.goal_id,
            intention_id=intention_id,
            transaction_id=transaction_id,
            thread_id=thread.thread_id if thread is not None else None,
            conversation_move=(
                thread.next_move.value
                if thread is not None and thread.next_move is not None else None
            ),
        )
        if not spoken:
            return False
        self._runner.commit_self_talk(parsed.text)
        self._director.mark_spoke(dec.action, now)
        return True

    async def _exec_forced_thread_park(
        self,
        dec: Any,
        now: float,
        thread: Any,
        *,
        intention_id: str | None = None,
        transaction_id: str | None = None,
    ) -> bool:
        """Deliver one explicit close when a continuation cannot add a fresh idea."""
        request_id = f"goal_close_{uuid.uuid4().hex[:8]}"
        context = (
            "[Conversation boundary: PARK]\n"
            f"Topic: {str(getattr(thread, 'topic', '') or '')[:240]}\n"
            f"Current summary: {str(getattr(thread, 'summary', '') or '')[:320]}\n"
            "Say one short closing statement that finishes this topic before Mai moves on. "
            "Do not add a new claim and do not ask a question. Only write Mai's spoken line."
        )
        context = _join_directives(context, self._speech_style_directive())
        parsed = await self._run_directed_deferred(request_id, context)
        request_id, parsed = await self._repair_speech_style(
            request_id,
            parsed,
            context,
            lambda retry_id, retry_context: self._run_directed_deferred(
                retry_id, retry_context,
            ),
        )
        spoken = await self._maybe_speak(
            request_id,
            parsed,
            dec.action,
            [],
            goal_id=dec.goal_id,
            intention_id=intention_id,
            transaction_id=transaction_id,
            thread_id=thread.thread_id,
            conversation_move="park",
        )
        if not spoken:
            return False
        self._runner.commit_self_talk(parsed.text)
        self._director.mark_spoke(dec.action, now)
        self._thread_forced_park_total += 1
        return True

    async def _exec_read(
        self, dec, now: float, *, transaction_id: str | None = None,
    ) -> bool:
        # SUMMARY/VIBE = react cả CĂN PHÒNG (không đáp 1 tin cụ thể) → đường ambient,
        # chỉ chỉ thị ở system, KHÔNG giả 1 user turn (giảm giọng meta).
        if dec.read_mode in (ReadMode.SUMMARY, ReadMode.VIBE):
            return await self._exec_room_reaction(
                dec, now, transaction_id=transaction_id,
            )

        req_id = f"read_{uuid.uuid4().hex[:8]}"
        refs = list(dec.refs)
        primary = refs[0] if refs else None
        # De-AI register: user turn = CHAT THẬT; "cách xử" (gộp/ack) → stage_direction (system).
        user_text = _read_user_text(dec)
        stage = _stage_direction_for(dec)
        stage = _join_directives(stage, self._behavior_directive(dec, user_text))
        if dec.action is DirectorAction.READ_CHAT:
            stage = _join_directives(stage, self._speech_style_directive())
        hist_text, commit_hist = _history_text_for(dec)
        parsed, _level = await self._run_turn_deferred(
            request_id=req_id,
            user_text=user_text,
            viewer_id=primary.viewer_id if primary else None,
            trigger_type="director_read",
            event_category=None,
            history_user_text=hist_text,
            commit_history=commit_hist,
            stage_direction=stage,
        )
        if dec.action is DirectorAction.READ_CHAT:
            self._speech_dedup_generated_total += 1
            if self._speech_candidate_is_duplicate(parsed):
                self._speech_dedup_duplicate_total += 1
                self._finalize_runner_delivery(req_id, False)
                replaced = False
                for attempt in range(self._speech_dedup_max_regenerations):
                    self._speech_dedup_regenerated_total += 1
                    retry_id = f"{req_id}_r{attempt + 1}"
                    retry_stage = _speech_dedup_correction_prompt(
                        stage or "",
                        str(getattr(parsed, "text", "")),
                        tuple(self._speech_dedup.recent()),
                    )
                    parsed, _level = await self._run_turn_deferred(
                        request_id=retry_id,
                        user_text=user_text,
                        viewer_id=primary.viewer_id if primary else None,
                        trigger_type="director_read",
                        event_category=None,
                        history_user_text=hist_text,
                        commit_history=commit_hist,
                        stage_direction=retry_stage,
                    )
                    self._speech_dedup_generated_total += 1
                    if not self._speech_candidate_is_duplicate(parsed):
                        req_id = retry_id
                        replaced = True
                        break
                    self._speech_dedup_duplicate_total += 1
                    self._finalize_runner_delivery(retry_id, False)
                if not replaced:
                    self._speech_dedup_suppressed_total += 1
                    self._quarantine_repetition_context(
                        refs=refs, thread_id=None, goal_id=dec.goal_id,
                    )
                    return False
            async def rerun_style(retry_id: str, retry_stage: str) -> Any:
                retry_parsed, _retry_level = await self._run_turn_deferred(
                    request_id=retry_id,
                    user_text=user_text,
                    viewer_id=primary.viewer_id if primary else None,
                    trigger_type="director_read",
                    event_category=None,
                    history_user_text=hist_text,
                    commit_history=commit_hist,
                    stage_direction=retry_stage,
                )
                return retry_parsed

            req_id, parsed = await self._repair_speech_style(
                req_id, parsed, stage or "", rerun_style,
            )
            if self._speech_candidate_is_duplicate(parsed):
                self._speech_dedup_duplicate_total += 1
                self._speech_dedup_suppressed_total += 1
                self._finalize_runner_delivery(req_id, False)
                self._quarantine_repetition_context(
                    refs=refs, thread_id=None, goal_id=dec.goal_id,
                )
                return False
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, refs, goal_id=dec.goal_id,
            transaction_id=transaction_id,
        )
        if not spoken:
            return False
        if dec.action is DirectorAction.READ_CHAT:
            self._focus_delivered_chat(refs)
        for r in refs:
            self._pool.remove(r.msg_id)
        self._turns_read += 1
        self._director.mark_spoke(dec.action, now)
        return True

    async def _exec_room_reaction(
        self, dec, now: float, *, transaction_id: str | None = None,
    ) -> bool:
        """SUMMARY/VIBE: Mai react không khí chat qua đường ambient (chỉ thị ở prompt,
        không user turn). Không commit history (không tin cụ thể)."""
        req_id = f"room_{uuid.uuid4().hex[:8]}"
        prompt = _join_directives(
            _room_reaction_prompt(dec), self._behavior_directive(dec),
            self._speech_style_directive(),
        )
        parsed = await self._run_ambient_deferred(req_id, prompt)
        self._room_reaction_generated_total += 1
        if self._room_candidate_is_duplicate(parsed):
            self._room_reaction_duplicate_total += 1
            self._finalize_runner_delivery(req_id, False)
            replaced = False
            for attempt in range(self._room_reaction_max_regenerations):
                self._room_reaction_regenerated_total += 1
                retry_id = f"{req_id}_r{attempt + 1}"
                retry_prompt = _room_reaction_correction_prompt(
                    prompt,
                    str(getattr(parsed, "text", "")),
                    tuple(self._room_reaction_dedup.recent()),
                )
                parsed = await self._run_ambient_deferred(retry_id, retry_prompt)
                self._room_reaction_generated_total += 1
                if not self._room_candidate_is_duplicate(parsed):
                    req_id = retry_id
                    replaced = True
                    break
                self._room_reaction_duplicate_total += 1
                self._finalize_runner_delivery(retry_id, False)
            if not replaced:
                self._room_reaction_suppressed_total += 1
                self._director.defer_room_reaction(
                    now + self._room_reaction_retry_defer_seconds,
                )
                return False
        req_id, parsed = await self._repair_speech_style(
            req_id,
            parsed,
            prompt,
            lambda retry_id, retry_prompt: self._run_ambient_deferred(
                retry_id, retry_prompt,
            ),
        )
        if self._room_candidate_is_duplicate(parsed):
            self._room_reaction_duplicate_total += 1
            self._room_reaction_suppressed_total += 1
            self._finalize_runner_delivery(req_id, False)
            self._director.defer_room_reaction(
                now + self._room_reaction_retry_defer_seconds,
            )
            return False
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, list(dec.refs),
            transaction_id=transaction_id,
        )
        if not spoken:
            return False
        self._clear_soft_continuations("room_reaction_delivered")
        self._room_reaction_dedup.record(str(getattr(parsed, "text", "")))
        self._director.mark_room_reaction(now)
        # SUMMARY clears low-score backlog; VIBE removes the reacted cluster.
        if dec.read_mode == ReadMode.SUMMARY:
            self._pool.purge_below(self._director.summary_ceiling, now)
        else:
            for r in list(dec.refs):
                self._pool.remove(r.msg_id)
        self._turns_read += 1
        self._director.mark_spoke(dec.action, now)
        return True

    async def _exec_self_talk(
        self, dec, now: float, *, transaction_id: str | None = None,
    ) -> bool:
        if self._autonomy is None and self._self_talk_planner is None:
            return False
        mood = self._current_mood()
        ctx = self._runtime_ctx_fn() if self._runtime_ctx_fn else RuntimeContext()
        proactive_thread = None
        if dec.proactive_source == "open_thread" and dec.proactive_summary:
            if self._agent_state is not None:
                try:
                    proactive_thread = next(
                        (item for item in self._agent_state.snapshot().open_threads
                         if item.thread_id == dec.proactive_source_id),
                        None,
                    )
                except Exception:
                    proactive_thread = None
            ctx = replace(
                ctx,
                working_memory_recent=[*ctx.working_memory_recent, dec.proactive_summary],
            )
        elif dec.proactive_source == "environment" and dec.proactive_summary:
            ctx = replace(ctx, environment_summary=dec.proactive_summary)
        planner_enabled = bool(
            self._self_talk_planner is not None and self._self_talk_planner.enabled
        )
        grounded = {
            "follow_up_topic", "environment_reaction", "roast_chat",
        }
        decision = None
        # The legacy content pool remains only as a grounded material adapter or
        # rollback path. Silence self-talk is composed from runtime state below.
        if self._autonomy is not None and (
            not planner_enabled or dec.proactive_category in grounded
        ):
            decision = (
                self._autonomy.force_generate_for(dec.proactive_category, mood, ctx)
                if dec.proactive_category else self._autonomy.force_generate(mood, ctx)
            )

        plan = None
        if planner_enabled:
            if dec.proactive_category in grounded and decision is None:
                return False
            plan = self._self_talk_planner.prepare(
                mood=mood,
                now=now,
                base_prompt=(
                    getattr(decision, "prompt_text", None)
                    if dec.proactive_category in grounded else None
                ),
                category=dec.proactive_category,
                tone_flags=self._tone_flags(),
                context=SelfTalkContext(
                    silence_seconds=max(0.0, float(ctx.silence_seconds)),
                    chat_count_recent=max(0, int(ctx.chat_count_last_10min)),
                    recent_context=tuple(ctx.working_memory_recent[-3:]),
                    environment_summary=ctx.environment_summary,
                ),
            )
            prompt_text = plan.prompt_text if plan is not None else None
        else:
            prompt_text = getattr(decision, "prompt_text", None)

        if not prompt_text:
            # Không có material/delivery thì tuyệt đối không cập nhật last-spoke.
            # Cooldown ở Director/proactive policy chặn spam quyết định thành công;
            # trạng thái phải phản ánh đúng rằng khán giả chưa nghe thấy gì.
            if planner_enabled:
                self._director.defer_self_talk(
                    now + self._self_talk_planner.unavailable_retry_seconds,
                )
            return False
        req_id = f"self_{uuid.uuid4().hex[:8]}"
        prompt = _join_directives(
            prompt_text, _proactive_thread_directive(proactive_thread),
            self._behavior_directive(dec),
        )
        delivery_req_id = req_id
        parsed = await self._run_ambient_deferred(req_id, prompt)
        if parsed.ok and parsed.text and self._autonomy is not None:
            if self._autonomy.check_dedup(parsed.text):
                rejected = parsed.text   # T2: bản trùng lặp = rejected
                self._finalize_runner_delivery(req_id, False)
                delivery_req_id = req_id + "_r"
                parsed = await self._run_ambient_deferred(
                    delivery_req_id, prompt_text,
                )
                # DPO pair: dedup regen (chosen = bản khác)
                try:
                    self._runner.log_pref_pair(
                        rejected, parsed.text, "dedup:ambient",
                        user_text=prompt_text)
                except Exception:
                    pass
        if plan is not None:
            if not self._self_talk_planner.can_deliver(plan.plan_id):
                self._finalize_runner_delivery(delivery_req_id, False)
                self._self_talk_planner.release(plan.plan_id)
                return False
            validation = self._self_talk_planner.validate_output(
                plan.plan_id, getattr(parsed, "text", ""),
            )
            if not validation.valid:
                rejected = getattr(parsed, "text", "")
                self._finalize_runner_delivery(delivery_req_id, False)
                delivery_req_id = req_id + "_shape"
                correction = _self_talk_correction_prompt(
                    prompt,
                    rejected,
                    max_sentences=plan.max_sentences,
                    allow_question=plan.allow_question,
                    require_question=plan.stage is SelfTalkStage.INVITE,
                    reasons=validation.reasons,
                )
                parsed = await self._run_ambient_deferred(delivery_req_id, correction)
                if not self._self_talk_planner.can_deliver(plan.plan_id):
                    self._finalize_runner_delivery(delivery_req_id, False)
                    self._self_talk_planner.release(plan.plan_id)
                    return False
                validation = self._self_talk_planner.validate_output(
                    plan.plan_id, getattr(parsed, "text", ""),
                )
                if not validation.valid:
                    self._finalize_runner_delivery(delivery_req_id, False)
                    self._self_talk_planner.release(plan.plan_id)
                    self._director.defer_self_talk(
                        now + self._self_talk_planner.unavailable_retry_seconds,
                    )
                    return False
                try:
                    self._runner.log_pref_pair(
                        rejected, parsed.text, "self_talk:shape",
                        user_text=prompt_text,
                    )
                except Exception:
                    pass
        self._speech_dedup_generated_total += 1
        if self._speech_candidate_is_duplicate(parsed):
            self._speech_dedup_duplicate_total += 1
            self._speech_dedup_suppressed_total += 1
            self._finalize_runner_delivery(delivery_req_id, False)
            if plan is not None:
                self._self_talk_planner.release(plan.plan_id)
                self._director.defer_self_talk(
                    now + self._self_talk_planner.unavailable_retry_seconds,
                )
            return False
        spoken = await self._maybe_speak(
            delivery_req_id, parsed, dec.action, [], transaction_id=transaction_id,
            thread_id=(
                proactive_thread.thread_id if proactive_thread is not None else None
            ),
            conversation_move=(
                proactive_thread.next_move.value
                if proactive_thread is not None and proactive_thread.next_move is not None
                else None
            ),
        )
        if not spoken:
            if plan is not None:
                self._self_talk_planner.release(plan.plan_id)
            return False
        if plan is not None:
            self._self_talk_planner.commit(plan.plan_id, parsed.text, now)
        if self._autonomy is not None:
            self._autonomy.on_self_spoke(parsed.text)
        self._runner.commit_self_talk(parsed.text)
        self._record_self_talk(delivery_req_id, parsed.text, now)
        self._turns_self += 1
        self._director.mark_spoke(dec.action, now)
        if spoken and dec.proactive_source:
            self._director.mark_proactive_used(dec, now)
        return True

    async def _exec_transition(
        self, dec, now: float, *, transaction_id: str | None = None,
    ) -> bool:
        req_id = f"trans_{uuid.uuid4().hex[:8]}"
        seg = self._director.current_segment()
        prompt = (
            f"[Context — Mai tự thông báo chuyển phần stream, KHÔNG phải trả lời chat]\n"
            f"- Đang chuyển từ phần '{seg.name}' ({seg.goal}) sang phần tiếp theo.\n"
            f"Nói 1 câu tự nhiên báo chuyển phần (kiểu 'thôi qua phần khác nào', "
            f"'sắp hết giờ rồi'), đúng giọng Mai. Chỉ viết thoại."
        )
        prompt = _join_directives(prompt, self._behavior_directive(dec))
        parsed = await self._run_ambient_deferred(req_id, prompt)
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, [], transaction_id=transaction_id,
        )
        if not spoken:
            return False
        self._runner.commit_self_talk(parsed.text)
        self._director.advance_segment(now)
        self._transitions += 1
        self._director.mark_spoke(DirectorAction.TRANSITION, now)
        return True

    # ---------- helpers ----------

    async def _maybe_speak(
        self,
        req_id: str,
        parsed: Any,
        action: Any,
        refs: list[Any],
        *,
        goal_id: str | None = None,
        intention_id: str | None = None,
        transaction_id: str | None = None,
        thread_id: str | None = None,
        conversation_move: str | None = None,
    ) -> bool:
        intention_id = intention_id or self._current_intention_id(goal_id)
        spoken = await self._delivery_boundary().deliver(
            req_id,
            parsed,
            action,
            refs,
            goal_id=goal_id,
            intention_id=intention_id,
            transaction_id=transaction_id,
            thread_id=thread_id,
            conversation_move=conversation_move,
        )
        if spoken:
            delivered_text = str(getattr(parsed, "text", ""))
            self._speech_dedup.record(delivered_text)
            self._speech_style.record(delivered_text)
        return spoken

    async def _run_turn_deferred(self, **kwargs: Any) -> Any:
        return await self._delivery_boundary().run_turn_deferred(**kwargs)

    async def _run_ambient_deferred(self, request_id: str, prompt: str) -> Any:
        return await self._delivery_boundary().run_ambient_deferred(request_id, prompt)

    async def _run_directed_deferred(self, request_id: str, context: str) -> Any:
        return await self._delivery_boundary().run_directed_deferred(request_id, context)

    def _finalize_runner_delivery(self, request_id: str, success: bool) -> None:
        self._delivery_boundary().finalize_runner_delivery(request_id, success)

    def _delivery_boundary(self) -> DirectorDeliveryBoundary:
        """Bind the helper to current attributes, including test-time replacements."""
        return DirectorDeliveryBoundary(
            runner=self._runner,
            speak=self._speak,
            transactions=self._transactions,
            animation=self._animation,
            embodiment_policy=self._embodiment_policy,
            action_adapter_boundary=self._action_adapter_boundary,
            mood_provider=self._current_mood,
            speech_completed=self._record_speech_completed,
            filter_rejected=self._quarantine_filter_rejection,
            logger=self._log,
        )

    def _quarantine_filter_rejection(
        self, *, refs: list[Any], thread_id: str | None, goal_id: str | None,
    ) -> None:
        """Drop an exhausted unsafe turn so Director cannot retry it forever."""
        if self._quarantine_context(
            refs=refs, thread_id=thread_id, goal_id=goal_id,
            reason="filter_rejected",
        ):
            self._filter_context_quarantined_total += 1

    def _quarantine_repetition_context(
        self, *, refs: list[Any], thread_id: str | None, goal_id: str | None,
    ) -> None:
        """Drop an exhausted duplicate context so it cannot retry every tick."""
        if self._quarantine_context(
            refs=refs, thread_id=thread_id, goal_id=goal_id,
            reason="speech_duplicate",
        ):
            self._speech_dedup_quarantined_total += 1

    def _quarantine_context(
        self, *, refs: list[Any], thread_id: str | None, goal_id: str | None,
        reason: str,
    ) -> bool:
        for ref in refs:
            msg_id = str(getattr(ref, "msg_id", "") or "")
            if msg_id:
                self._pool.remove(msg_id)

        resolved_ids: set[str] = set()
        if thread_id:
            resolved_ids.add(thread_id)
        if refs and self._thread_manager is not None and self._agent_state is not None:
            source_ids = {
                f"agent:chat:{getattr(ref, 'msg_id', '')}"
                for ref in refs if getattr(ref, "msg_id", None)
            }
            try:
                resolved_ids.update(
                    thread.thread_id
                    for thread in self._agent_state.snapshot().open_threads
                    if thread.origin_event_id in source_ids
                )
            except Exception as exc:
                self._log.warning("director_filter_thread_lookup_failed", error=str(exc))

        changed = False
        if self._thread_manager is not None:
            for rejected_thread_id in resolved_ids:
                try:
                    changed = bool(self._thread_manager.resolve(
                        rejected_thread_id, reason=reason,
                    )) or changed
                except Exception as exc:
                    self._log.warning(
                        "director_context_thread_resolve_failed",
                        thread_id=rejected_thread_id,
                        reason=reason,
                        error=str(exc),
                    )
        if goal_id and self._goal_manager is not None:
            try:
                changed = bool(self._goal_manager.cancel(
                    goal_id, reason=reason,
                )) or changed
            except Exception as exc:
                self._log.warning(
                    "director_context_goal_cancel_failed", goal_id=goal_id,
                    reason=reason,
                    error=str(exc),
                )
        if refs or resolved_ids or goal_id:
            self._log.info(
                "director_context_quarantined",
                thread_ids=sorted(resolved_ids), goal_id=goal_id,
                refs=[str(getattr(ref, "msg_id", "")) for ref in refs],
                state_changed=changed, reason=reason,
            )
            return True
        return False

    def _focus_delivered_chat(self, refs: list[Any]) -> None:
        """Focus only the thread backed by the targeted chat that was delivered."""
        if self._goal_manager is None:
            return
        raw_ids = {
            str(getattr(ref, "msg_id", "") or "") for ref in refs
            if str(getattr(ref, "msg_id", "") or "")
        }
        source_ids = {*raw_ids, *(f"agent:chat:{value}" for value in raw_ids)}
        parent_id = None
        if self._agent_state is not None and source_ids:
            try:
                matches = [
                    thread for thread in self._agent_state.snapshot().open_threads
                    if (
                        thread.origin_event_id in source_ids
                        or any(
                            evidence.source_event_id in source_ids
                            for evidence in thread.evidence
                        )
                    )
                ]
                if matches:
                    parent_id = max(
                        matches, key=lambda item: (item.updated_at, item.thread_id),
                    ).thread_id
            except Exception as exc:
                self._log.warning("director_thread_focus_lookup_failed", error=str(exc))
        try:
            changed = int(self._goal_manager.focus_delivered_thread(
                parent_id,
                source_event_ids=source_ids,
            ))
            if changed:
                self._thread_focus_total += 1
        except Exception as exc:
            self._log.warning("director_thread_focus_failed", error=str(exc))

    def _clear_soft_continuations(self, reason: str) -> None:
        if self._goal_manager is None:
            return
        try:
            cleared = int(self._goal_manager.clear_continue_threads(reason=reason))
            if cleared:
                self._thread_boundary_clear_total += cleared
        except Exception as exc:
            self._log.warning("director_thread_boundary_clear_failed", error=str(exc))

    @staticmethod
    def _idempotency_key(dec: Any, now: float) -> str:
        refs = ",".join(str(getattr(ref, "msg_id", "")) for ref in dec.refs)
        stable_ref = refs or str(getattr(dec, "goal_id", "") or "")
        if not stable_ref:
            stable_ref = f"{getattr(dec, 'segment', '')}:{now:.6f}"
        return f"{dec.action.value}:{stable_ref}"

    def _behavior_directive(self, dec: Any, query: str = "") -> str | None:
        if self._behavior_library is None:
            return None
        repair_kind = None
        if query and self._repair_policy is not None and self._agent_state is not None:
            try:
                repair = self._repair_policy.decide(self._agent_state.snapshot(), query)
                repair_kind = repair.kind.value if repair is not None else None
            except Exception:
                repair_kind = None
        try:
            selected = self._behavior_library.select(
                dec.action.value,
                self._current_mood(),
                self._tone_flags(),
                proactive_source=dec.proactive_source,
                repair_kind=repair_kind,
            )
        except Exception:
            return None
        if not selected.applicable or not selected.directive:
            return None
        return f"Behavior [{selected.kind.value}]: {selected.directive}"

    def _record_speech_completed(
        self, request_id: str, action: Any, refs: list[Any], *,
        goal_id: str | None = None, intention_id: str | None = None,
        text: str = "", thread_id: str | None = None,
        conversation_move: str | None = None,
    ) -> None:
        if self._agent_state is None:
            return
        try:
            snapshot = self._agent_state.snapshot()
            effective_goal_id = goal_id or snapshot.active_goal_ref
            effective_intention_id = intention_id or self._current_intention_id(
                effective_goal_id,
            )
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:speech_completed:{request_id}",
                kind=AgentEventKind.SPEECH_COMPLETED,
                source=AgentEventSource.DIRECTOR,
                timestamp=_timestamp(self._clock()),
                confidence=1.0,
                payload={
                    "action": getattr(action, "value", str(action)),
                    "goal_id": effective_goal_id,
                    "intention_id": effective_intention_id,
                    "ref_event_ids": [getattr(ref, "msg_id", "") for ref in refs],
                    "text": text,
                    "thread_id": thread_id,
                    "conversation_move": conversation_move,
                    "expects_chat_answer": (
                        getattr(action, "value", str(action)) == "ask_follow_up"
                        or conversation_move == "invite"
                    ),
                },
                provenance=EventProvenance(
                    producer="director_loop", source_event_id=request_id,
                    session_id=getattr(self._runner, "session_id", None),
                ),
            ))
        except Exception as exc:
            self._log.warning("director_speech_complete_event_failed", error=str(exc))

    def _record_director_action(self, dec: Any, now: float) -> None:
        if self._agent_state is None:
            return

        segment = self._director.current_segment()
        phase = str(getattr(segment, "name", "main")).lower()
        if phase not in {"opening", "main", "chat", "closing"}:
            phase = "main"
        try:
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:director:{uuid.uuid4().hex}",
                kind=AgentEventKind.DIRECTOR_ACTION,
                source=AgentEventSource.DIRECTOR,
                timestamp=_timestamp(now),
                confidence=1.0,
                payload={
                    "action": dec.action.value,
                    "read_mode": getattr(getattr(dec, "read_mode", None), "value", None),
                    "ref_event_ids": [getattr(ref, "msg_id", "") for ref in dec.refs],
                    "stream_phase": phase,
                    "goal_id": getattr(dec, "goal_id", None),
                },
                provenance=EventProvenance(producer="director_loop"),
            ))
        except Exception as exc:
            self._log.warning("director_agent_event_failed", error=str(exc))

    def _record_director_metric(self, dec: Any) -> None:
        if dec.reason == "below_actionable_score":
            self._chat_suppressed_total += 1
        if dec.reason == "room_reaction_cooldown":
            self._room_reaction_cooldown_blocked_total += 1
        if self._metrics is not None:
            try:
                self._metrics.record_director_action(dec.action.value, dec.reason)
            except Exception:
                pass

    def _mark_trajectory_selection(self, trajectory_id: str | None, owner: str) -> None:
        if trajectory_id is None or self._trajectory_records is None:
            return
        try:
            self._trajectory_records.mark_selection(trajectory_id, owner=owner)
        except Exception as exc:
            self._log.warning("trajectory_selection_failed", error=str(exc))

    def _record_trajectory_action(
        self,
        decision_id: str | None,
        dec: Any,
        director_input: DirectorInput,
        now: float,
    ) -> None:
        if decision_id is None or self._trajectory_records is None:
            return
        trajectory_id = self._trajectory_by_decision.get(decision_id)
        if trajectory_id is None:
            return
        try:
            requested_at = datetime.fromtimestamp(float(now), timezone.utc)
            evidence = tuple(dict.fromkeys((
                *(str(getattr(ref, "msg_id", "")) for ref in dec.refs),
                *(str(item) for item in dec.proactive_evidence_ids),
            )))
            evidence = tuple(item for item in evidence if item)
            action_type = str(dec.action.value).upper()
            request = ActionRequest(
                schema_version=1,
                action_id=f"trajectory:{trajectory_id}",
                capability_id=action_type,
                action_type=action_type,
                target=None,
                arguments={},
                intention_id=self._decision_intention_id(dec, director_input),
                evidence_refs=evidence,
                idempotency_key=f"trajectory:{trajectory_id}",
                priority=0.0,
                requested_at=requested_at,
                transaction_policy="delivery_aware",
            )
            self._trajectory_records.record_action(trajectory_id, request)
            self._trajectory_requested_at[trajectory_id] = requested_at
        except Exception as exc:
            self._log.warning("trajectory_action_failed", error=str(exc))

    def _record_trajectory_no_action(
        self, decision_id: str | None, reason_code: str,
    ) -> None:
        if decision_id is None or self._trajectory_records is None:
            return
        trajectory_id = self._trajectory_by_decision.pop(decision_id, None)
        if trajectory_id is None:
            return
        try:
            self._trajectory_records.record_no_action(
                trajectory_id, reason_code=str(reason_code),
            )
        except Exception as exc:
            self._log.warning("trajectory_no_action_failed", error=str(exc))

    def _record_trajectory_result(
        self,
        decision_id: str,
        *,
        delivery_state: str,
        outcome: str,
    ) -> None:
        if self._trajectory_records is None:
            return
        trajectory_id = self._trajectory_by_decision.pop(decision_id, None)
        if trajectory_id is None:
            return
        started_at = self._trajectory_requested_at.pop(
            trajectory_id,
            datetime.fromtimestamp(float(self._clock()), timezone.utc),
        )
        try:
            completed_at = datetime.fromtimestamp(float(self._clock()), timezone.utc)
            if completed_at < started_at:
                completed_at = started_at
            verified = outcome in {"committed", "completed"} and delivery_state == "delivered"
            if verified:
                status = "success"
            elif delivery_state == "cancelled":
                status = "cancelled"
            elif outcome == "duplicate_committed":
                status = "rejected"
            else:
                status = "failed"
            result = ActionResult(
                schema_version=1,
                action_id=f"trajectory:{trajectory_id}",
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                verified=verified,
                verification_source="director_delivery" if verified else None,
                result_data={},
                error_code=None if verified else str(outcome),
            )
            verification = VerificationResult(
                verified=verified,
                source="director_delivery",
                reason_code=str(outcome),
                evidence_refs=(),
            )
            self._trajectory_records.record_result(
                trajectory_id, result, verification,
            )
        except Exception as exc:
            self._log.warning("trajectory_result_failed", error=str(exc))

    def _record_decision(
        self, dec: Any, director_input: DirectorInput, now: float,
    ) -> str | None:
        if self._decision_records is None:
            return None
        try:
            refs = tuple(dict.fromkeys((
                *(str(getattr(ref, "msg_id", "")) for ref in dec.refs),
                *(str(item) for item in dec.proactive_evidence_ids),
            )))
            active_goal = director_input.goals.active
            summary = DecisionCandidateSummary(
                candidate_count=len(director_input.chat_candidates),
                pool_size=director_input.pool_size,
                pulse_state=director_input.pulse_state,
                active_goal_id=(active_goal.goal_id if active_goal is not None else None),
                safety_hold=director_input.safety_hold,
                candidate_kinds=tuple(
                    str(item.kind) for item in director_input.chat_candidates
                ),
                top_score=(
                    director_input.chat_candidates[0].score
                    if director_input.chat_candidates else None
                ),
            )
            rejection = self._decision_records.classify_hard_rejection(
                dec.action.value, dec.reason,
            )
            record = self._decision_records.record_decision(
                created_at=now,
                action=dec.action.value,
                reason=dec.reason,
                segment=dec.segment,
                evidence_refs=refs,
                candidate_summary=summary,
                hard_rejection_reason=rejection,
            )
            if (
                record is not None
                and getattr(dec, "decision_owner", "legacy") == "director_v2"
                and getattr(dec, "director_v2_proposal_id", None)
            ):
                self._trajectory_by_decision[record.decision_id] = str(
                    dec.director_v2_proposal_id,
                )
            return record.decision_id if record is not None else None
        except Exception as exc:
            self._log.warning("decision_record_failed", error=str(exc))
            return None

    def _update_decision_transaction(
        self,
        decision_id: str | None,
        transaction: Any,
        *,
        delivery_state: str,
        outcome: str,
    ) -> None:
        if self._decision_records is None or decision_id is None:
            return
        try:
            state = getattr(transaction, "state", "")
            self._decision_records.update_transaction(
                decision_id,
                transaction_id=str(getattr(transaction, "transaction_id", "")),
                transaction_state=getattr(state, "value", str(state)),
                delivery_state=delivery_state,
                outcome=outcome,
            )
            if outcome in {
                "committed", "duplicate_committed", "released",
            }:
                self._record_trajectory_result(
                    decision_id,
                    delivery_state=delivery_state,
                    outcome=outcome,
                )
        except Exception as exc:
            self._log.warning("decision_record_update_failed", error=str(exc))

    def _update_decision_outcome(
        self,
        decision_id: str | None,
        *,
        delivery_state: str,
        outcome: str,
    ) -> None:
        if self._decision_records is None or decision_id is None:
            return
        try:
            self._decision_records.update_outcome(
                decision_id, delivery_state=delivery_state, outcome=outcome,
            )
            self._record_trajectory_result(
                decision_id,
                delivery_state=delivery_state,
                outcome=outcome,
            )
        except Exception as exc:
            self._log.warning("decision_record_update_failed", error=str(exc))

    def decision_snapshot(self) -> dict[str, Any] | None:
        if self._decision_records is None:
            return None
        try:
            return self._decision_records.snapshot()
        except Exception:
            return None

    def _record_self_talk(self, request_id: str, text: str, now: float) -> None:
        if self._agent_state is None:
            return
        try:
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:self_talk:{request_id}",
                kind=AgentEventKind.SELF_TALK_COMPLETED,
                source=AgentEventSource.AUTONOMY,
                timestamp=_timestamp(now),
                confidence=1.0,
                payload={"text": text},
                provenance=EventProvenance(
                    producer="director_loop", source_event_id=request_id,
                ),
            ))
        except Exception as exc:
            self._log.warning("self_talk_agent_event_failed", error=str(exc))

    def _current_mood(self) -> MoodState:
        if self._emotion is not None:
            try:
                return self._emotion.current_mood()
            except Exception:
                pass
        return MoodState()

    def _room_candidate_is_duplicate(self, parsed: Any) -> bool:
        """Check only filter-eligible text; exhausted filter remains the delivery gate."""
        verdict = getattr(self._runner, "last_filter_verdict", None)
        if verdict is not None and getattr(verdict, "passed", True) is not True:
            return False
        return self._room_reaction_dedup.check(str(getattr(parsed, "text", "")))

    def _speech_candidate_is_duplicate(self, parsed: Any) -> bool:
        """Check filtered public speech against delivered output only."""
        verdict = getattr(self._runner, "last_filter_verdict", None)
        if verdict is not None and getattr(verdict, "passed", True) is not True:
            return False
        return self._speech_dedup.check(str(getattr(parsed, "text", "")))

    def _speech_style_directive(
        self, *, question_budget_exempt: bool = False,
    ) -> str | None:
        forbidden, avoid_question = self._speech_style.constraints(
            question_budget_exempt=question_budget_exempt,
        )
        return _speech_style_constraint_prompt(
            forbidden,
            avoid_question=avoid_question,
            max_sentences=self._speech_style.max_sentences,
            max_words=self._speech_style.max_words,
        )

    def _speech_style_assessment(
        self,
        parsed: Any,
        *,
        question_budget_exempt: bool = False,
    ) -> SpeechStyleAssessment:
        verdict = getattr(self._runner, "last_filter_verdict", None)
        if verdict is not None and getattr(verdict, "passed", True) is not True:
            return SpeechStyleAssessment((), None, False)
        return self._speech_style.assess(
            str(getattr(parsed, "text", "")),
            question_budget_exempt=question_budget_exempt,
        )

    async def _repair_speech_style(
        self,
        request_id: str,
        parsed: Any,
        original_context: str,
        rerun: Callable[[str, str], Awaitable[Any]],
        *,
        question_budget_exempt: bool = False,
    ) -> tuple[str, Any]:
        """Try one bounded style-only rewrite and fail open when still formulaic."""
        assessment = self._speech_style_assessment(
            parsed, question_budget_exempt=question_budget_exempt,
        )
        if assessment.valid:
            return request_id, parsed
        self._speech_style_violation_total += 1
        current_id = request_id
        current = parsed
        for attempt in range(self._speech_style_max_regenerations):
            self._finalize_runner_delivery(current_id, False)
            self._speech_style_regenerated_total += 1
            retry_id = f"{request_id}_s{attempt + 1}"
            retry_context = _speech_style_correction_prompt(
                original_context,
                str(getattr(current, "text", "")),
                reasons=assessment.reasons,
                opener=assessment.opener,
                max_sentences=self._speech_style.max_sentences,
                max_words=self._speech_style.max_words,
            )
            current = await rerun(retry_id, retry_context)
            current_id = retry_id
            assessment = self._speech_style_assessment(
                current, question_budget_exempt=question_budget_exempt,
            )
            if assessment.valid:
                return current_id, current
            self._speech_style_violation_total += 1
        if "sentence_budget" in assessment.reasons or "word_budget" in assessment.reasons:
            clamped = self._speech_style.clamp_shape(
                str(getattr(current, "text", "")),
            )
            if clamped != str(getattr(current, "text", "")):
                if hasattr(current, "model_copy"):
                    current = current.model_copy(update={"text": clamped})
                else:
                    current = replace(current, text=clamped)
                self._speech_style_clamped_total += 1
                assessment = self._speech_style_assessment(
                    current, question_budget_exempt=question_budget_exempt,
                )
                if assessment.valid:
                    return current_id, current
        self._speech_style_exhausted_total += 1
        return current_id, current

    def get_metrics(self) -> dict[str, Any]:
        planner_metrics = (
            self._self_talk_planner.get_metrics()
            if self._self_talk_planner is not None else {}
        )
        return {
            "director_turns_read": self._turns_read,
            "director_turns_self": self._turns_self,
            "director_transitions_run": self._transitions,
            **self._director.get_metrics(),
            "director_chat_suppressed_total": self._chat_suppressed_total,
            "director_filter_context_quarantined_total": (
                self._filter_context_quarantined_total
            ),
            "director_execute_failed_total": self._execute_failed_total,
            "director_v2_primary_selected_total": (
                self._director_v2_primary_selected_total
            ),
            "director_v2_primary_fallback_total": (
                self._director_v2_primary_fallback_total
            ),
            "director_v2_hard_preemption_total": (
                self._director_v2_hard_preemption_total
            ),
            "director_room_reaction_generated_total": (
                self._room_reaction_generated_total
            ),
            "director_room_reaction_duplicate_total": (
                self._room_reaction_duplicate_total
            ),
            "director_room_reaction_regenerated_total": (
                self._room_reaction_regenerated_total
            ),
            "director_room_reaction_suppressed_total": (
                self._room_reaction_suppressed_total
            ),
            "director_room_reaction_cooldown_blocked_total": (
                self._room_reaction_cooldown_blocked_total
            ),
            "director_room_reaction_recent_count": len(
                self._room_reaction_dedup.recent()
            ),
            "director_speech_dedup_generated_total": (
                self._speech_dedup_generated_total
            ),
            "director_speech_dedup_duplicate_total": (
                self._speech_dedup_duplicate_total
            ),
            "director_speech_dedup_regenerated_total": (
                self._speech_dedup_regenerated_total
            ),
            "director_speech_dedup_suppressed_total": (
                self._speech_dedup_suppressed_total
            ),
            "director_speech_dedup_quarantined_total": (
                self._speech_dedup_quarantined_total
            ),
            "director_speech_dedup_recent_count": len(self._speech_dedup.recent()),
            "director_speech_style_violation_total": (
                self._speech_style_violation_total
            ),
            "director_speech_style_regenerated_total": (
                self._speech_style_regenerated_total
            ),
            "director_speech_style_exhausted_total": (
                self._speech_style_exhausted_total
            ),
            "director_speech_style_clamped_total": (
                self._speech_style_clamped_total
            ),
            "director_thread_focus_total": self._thread_focus_total,
            "director_thread_boundary_clear_total": self._thread_boundary_clear_total,
            "director_thread_forced_park_total": self._thread_forced_park_total,
            "director_speech_style_recent_count": self._speech_style.recent_count(),
            **planner_metrics,
        }
