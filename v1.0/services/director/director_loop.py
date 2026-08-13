"""DirectorLoop — turn driver duy nhất (C0.4, docs/03_COMPONENT_REFERENCE.md §C0.4 "hợp nhất kiến trúc").

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
from typing import Any, Awaitable, Callable

from interfaces.animation import MoodState
from interfaces.decision_record import DecisionCandidateSummary
from interfaces.self_talk import SelfTalkContext, SelfTalkStage
from orchestrator.logger import get_logger
from services.autonomy.material_provider import RuntimeContext
from services.director.chat_pulse import PulseState
from services.director.director import Director, DirectorAction, ReadMode
from services.director.action_context import ActionContextBuilder
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.delivery_boundary import DirectorDeliveryBoundary
from services.director.action_prompts import (
    history_text_for as _history_text_for,
    join_directives as _join_directives,
    proactive_thread_directive as _proactive_thread_directive,
    read_user_text as _read_user_text,
    room_reaction_prompt as _room_reaction_prompt,
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
        self_talk_planner: Any = None,
        thread_manager: Any = None,
        animation: Any = None,
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
        self._self_talk_planner = self_talk_planner
        self._thread_manager = thread_manager
        self._animation = animation

        self._task: asyncio.Task | None = None
        self._running = False
        self._log = get_logger("director_loop")
        self._turns_read = 0
        self._turns_self = 0
        self._transitions = 0
        self._chat_suppressed_total = 0
        self._last_pulse_state: PulseState | None = None   # Task7 edge debounce
        self._pulse_mood_pushes = 0
        self._filter_context_quarantined_total = 0

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
        # TASK 6: cập nhật baseline_tempo mỗi tick → accel phản ánh thật (không kẹt 1.0)
        try:
            self._pulse.update_baseline(now)
        except Exception:
            pass
        # TASK 7: ChatPulse → mood. Chỉ đẩy khi state CHUYỂN sang hype/lively (edge,
        # debounce — không spam mỗi tick).
        await self._maybe_push_pulse_mood(now)
        urge_ready = False
        if self._autonomy is not None:
            try:
                urge_ready = self._autonomy.urge.should_speak_now()
            except Exception:
                pass
        director_input = self._build_director_input(now, urge_ready)
        dec = self._director.decide(director_input)
        self._record_director_metric(dec)
        decision_id = self._record_decision(dec, director_input, now)

        if dec.action == DirectorAction.WAIT:
            self._record_director_action(dec, now)
            return dec.action

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
                else:
                    self._update_decision_outcome(
                        decision_id,
                        delivery_state="delivered" if committed else "failed",
                        outcome="completed" if committed else "not_delivered",
                    )
                self._record_director_action(dec, now)
            except Exception as e:
                if transaction_id is not None:
                    try:
                        transaction = self._transactions.release(transaction_id, str(e))
                        self._update_decision_transaction(
                            decision_id, transaction,
                            delivery_state="failed", outcome="released",
                        )
                    except Exception:
                        pass
                else:
                    self._update_decision_outcome(
                        decision_id,
                        delivery_state="failed",
                        outcome="execution_failed",
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
        """Task7: khi pulse chuyển sang HYPE_SPAM/LIVELY (edge) → 1 EmotionEvent
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
        req_id = f"goal_{uuid.uuid4().hex[:8]}"
        parsed = await self._run_directed_deferred(req_id, context)
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, [], goal_id=dec.goal_id,
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
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, refs, goal_id=dec.goal_id,
            transaction_id=transaction_id,
        )
        if not spoken:
            return False
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
        )
        parsed = await self._run_ambient_deferred(req_id, prompt)
        spoken = await self._maybe_speak(
            req_id, parsed, dec.action, list(dec.refs),
            transaction_id=transaction_id,
        )
        if not spoken:
            return False
        # SUMMARY dọn backlog điểm thấp (Task 3); VIBE gỡ cụm đã react
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
        transaction_id: str | None = None,
        thread_id: str | None = None,
        conversation_move: str | None = None,
    ) -> bool:
        return await self._delivery_boundary().deliver(
            req_id,
            parsed,
            action,
            refs,
            goal_id=goal_id,
            transaction_id=transaction_id,
            thread_id=thread_id,
            conversation_move=conversation_move,
        )

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
            mood_provider=self._current_mood,
            speech_completed=self._record_speech_completed,
            filter_rejected=self._quarantine_filter_rejection,
            logger=self._log,
        )

    def _quarantine_filter_rejection(
        self, *, refs: list[Any], thread_id: str | None, goal_id: str | None,
    ) -> None:
        """Drop an exhausted unsafe turn so Director cannot retry it forever."""
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
                        rejected_thread_id, reason="filter_rejected",
                    )) or changed
                except Exception as exc:
                    self._log.warning(
                        "director_filter_thread_resolve_failed",
                        thread_id=rejected_thread_id,
                        error=str(exc),
                    )
        if goal_id and self._goal_manager is not None:
            try:
                changed = bool(self._goal_manager.cancel(
                    goal_id, reason="filter_rejected",
                )) or changed
            except Exception as exc:
                self._log.warning(
                    "director_filter_goal_cancel_failed", goal_id=goal_id,
                    error=str(exc),
                )
        if refs or resolved_ids or goal_id:
            self._filter_context_quarantined_total += 1
            self._log.info(
                "director_filter_context_quarantined",
                thread_ids=sorted(resolved_ids), goal_id=goal_id,
                refs=[str(getattr(ref, "msg_id", "")) for ref in refs],
                state_changed=changed,
            )

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
        goal_id: str | None = None, text: str = "", thread_id: str | None = None,
        conversation_move: str | None = None,
    ) -> None:
        if self._agent_state is None:
            return
        try:
            snapshot = self._agent_state.snapshot()
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:speech_completed:{request_id}",
                kind=AgentEventKind.SPEECH_COMPLETED,
                source=AgentEventSource.DIRECTOR,
                timestamp=_timestamp(self._clock()),
                confidence=1.0,
                payload={
                    "action": getattr(action, "value", str(action)),
                    "goal_id": goal_id or snapshot.active_goal_ref,
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
        if self._metrics is not None:
            try:
                self._metrics.record_director_action(dec.action.value, dec.reason)
            except Exception:
                pass

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
            **planner_metrics,
        }
