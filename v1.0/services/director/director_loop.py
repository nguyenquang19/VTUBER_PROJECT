"""DirectorLoop — turn driver duy nhất (C0.4, ROADMAP §C0.4 "hợp nhất kiến trúc").

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
from typing import Any, Awaitable, Callable

from interfaces.animation import MoodState
from orchestrator.logger import get_logger
from services.autonomy.material_provider import RuntimeContext
from services.director.chat_pulse import PulseState
from services.director.director import Director, DirectorAction, ReadMode
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)

SpeakFn = Callable[[str, str], Awaitable[None]]


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

        self._task: asyncio.Task | None = None
        self._running = False
        self._log = get_logger("director_loop")
        self._turns_read = 0
        self._turns_self = 0
        self._transitions = 0
        self._last_pulse_state: PulseState | None = None   # Task7 edge debounce
        self._pulse_mood_pushes = 0

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
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
        dec = self._director.decide(now, urge_ready=urge_ready)

        if dec.action == DirectorAction.WAIT:
            return dec.action

        async with self._turn_lock:
            try:
                await self._execute(dec, now)
                self._record_director_action(dec, now)
            except Exception as e:
                self._log.warning("director_execute_failed",
                                  action=dec.action.value, error=str(e))
        return dec.action

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

    async def _execute(self, dec, now: float) -> None:
        if dec.action in (DirectorAction.READ_CHAT, DirectorAction.ACK_DONATION):
            await self._exec_read(dec, now)
        elif dec.action in (DirectorAction.SELF_TALK, DirectorAction.FOLLOW_UP):
            await self._exec_self_talk(dec, now)
        elif dec.action == DirectorAction.TRANSITION:
            await self._exec_transition(dec, now)

    async def _exec_read(self, dec, now: float) -> None:
        # SUMMARY/VIBE = react cả CĂN PHÒNG (không đáp 1 tin cụ thể) → đường ambient,
        # chỉ chỉ thị ở system, KHÔNG giả 1 user turn (giảm giọng meta).
        if dec.read_mode in (ReadMode.SUMMARY, ReadMode.VIBE):
            await self._exec_room_reaction(dec, now)
            return

        req_id = f"read_{uuid.uuid4().hex[:8]}"
        refs = list(dec.refs)
        primary = refs[0] if refs else None
        # De-AI register: user turn = CHAT THẬT; "cách xử" (gộp/ack) → stage_direction (system).
        user_text = _read_user_text(dec)
        stage = _stage_direction_for(dec)
        hist_text, commit_hist = _history_text_for(dec)
        parsed, _level = await self._runner.run_turn(
            request_id=req_id,
            user_text=user_text,
            viewer_id=primary.viewer_id if primary else None,
            trigger_type="director_read",
            event_category=None,
            history_user_text=hist_text,
            commit_history=commit_hist,
            stage_direction=stage,
        )
        for r in refs:
            self._pool.remove(r.msg_id)
        self._turns_read += 1
        self._director.mark_spoke(dec.action, now)
        await self._maybe_speak(req_id, parsed, dec.action, refs)

    async def _exec_room_reaction(self, dec, now: float) -> None:
        """SUMMARY/VIBE: Mai react không khí chat qua đường ambient (chỉ thị ở prompt,
        không user turn). Không commit history (không tin cụ thể)."""
        req_id = f"room_{uuid.uuid4().hex[:8]}"
        prompt = _room_reaction_prompt(dec)
        parsed = await self._runner.run_ambient_turn(req_id, prompt)
        # SUMMARY dọn backlog điểm thấp (Task 3); VIBE gỡ cụm đã react
        if dec.read_mode == ReadMode.SUMMARY:
            self._pool.purge_below(self._director.summary_ceiling, now)
        else:
            for r in list(dec.refs):
                self._pool.remove(r.msg_id)
        self._turns_read += 1
        self._director.mark_spoke(dec.action, now)
        await self._maybe_speak(req_id, parsed, dec.action, list(dec.refs))

    async def _exec_self_talk(self, dec, now: float) -> None:
        if self._autonomy is None:
            self._director.mark_spoke(dec.action, now)
            return
        mood = self._current_mood()
        ctx = self._runtime_ctx_fn() if self._runtime_ctx_fn else RuntimeContext()
        decision = self._autonomy.force_generate(mood, ctx)
        if decision is None:
            # không có material → coi như đã "nói" để reset dead-air, tránh spin
            self._director.mark_spoke(dec.action, now)
            return
        req_id = f"self_{uuid.uuid4().hex[:8]}"
        parsed = await self._runner.run_ambient_turn(req_id, decision.prompt_text)
        if parsed.ok and parsed.text:
            if self._autonomy.check_dedup(parsed.text):
                rejected = parsed.text   # T2: bản trùng lặp = rejected
                parsed = await self._runner.run_ambient_turn(req_id + "_r", decision.prompt_text)
                # DPO pair: dedup regen (chosen = bản khác)
                try:
                    self._runner.log_pref_pair(
                        rejected, parsed.text, "dedup:ambient",
                        user_text=decision.prompt_text)
                except Exception:
                    pass
            self._autonomy.on_self_spoke(parsed.text)
            self._runner.commit_self_talk(parsed.text)
            self._record_self_talk(req_id, parsed.text, now)
        self._turns_self += 1
        self._director.mark_spoke(dec.action, now)
        await self._maybe_speak(req_id, parsed, dec.action, [])

    async def _exec_transition(self, dec, now: float) -> None:
        req_id = f"trans_{uuid.uuid4().hex[:8]}"
        seg = self._director.current_segment()
        prompt = (
            f"[Context — Mai tự thông báo chuyển phần stream, KHÔNG phải trả lời chat]\n"
            f"- Đang chuyển từ phần '{seg.name}' ({seg.goal}) sang phần tiếp theo.\n"
            f"Nói 1 câu tự nhiên báo chuyển phần (kiểu 'thôi qua phần khác nào', "
            f"'sắp hết giờ rồi'), đúng giọng Mai. Chỉ viết thoại."
        )
        parsed = await self._runner.run_ambient_turn(req_id, prompt)
        if parsed.ok and parsed.text:
            self._runner.commit_self_talk(parsed.text)
        self._director.advance_segment(now)
        self._transitions += 1
        self._director.mark_spoke(DirectorAction.TRANSITION, now)
        await self._maybe_speak(req_id, parsed, dec.action, [])

    # ---------- helpers ----------

    async def _maybe_speak(self, req_id: str, parsed, action: Any, refs: list[Any]) -> None:
        if not getattr(parsed, "ok", False) or not getattr(parsed, "text", ""):
            return
        if self._speak is not None:
            try:
                await self._speak(req_id, parsed.text)
            except Exception as e:
                self._log.warning("director_speak_failed", error=str(e))
                return
        self._record_speech_completed(req_id, action, refs)

    def _record_speech_completed(self, request_id: str, action: Any, refs: list[Any]) -> None:
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
                    "goal_id": snapshot.active_goal_ref,
                    "ref_event_ids": [getattr(ref, "msg_id", "") for ref in refs],
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
                },
                provenance=EventProvenance(producer="director_loop"),
            ))
        except Exception as exc:
            self._log.warning("director_agent_event_failed", error=str(exc))

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
        return {
            "director_turns_read": self._turns_read,
            "director_turns_self": self._turns_self,
            "director_transitions_run": self._transitions,
            **self._director.get_metrics(),
        }


def _read_user_text(dec) -> str:
    """USER turn = CHAT THẬT (SINGLE/CLUSTER/ACK). Không nhét chỉ thị vào đây."""
    refs = dec.refs
    if not refs:
        return ""
    if dec.read_mode == ReadMode.CLUSTER and len(refs) >= 1:
        return " / ".join(r.text for r in refs[:3])   # mấy tin cùng chủ đề, text thật
    return refs[0].text   # SINGLE / ACK: text chat thật


def _timestamp(value: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _stage_direction_for(dec) -> str | None:
    """Chỉ thị "cách xử" lượt này → đặt ở SYSTEM (không phải user turn)."""
    refs = dec.refs
    if dec.read_mode == ReadMode.ACK and refs:
        r = refs[0]
        who = r.viewer_name or r.viewer_id or "một người"
        return f"{who} vừa SUPERCHAT (ủng hộ tiền) — ack ngay, cảm ơn tự nhiên đúng giọng Mai."
    if dec.read_mode == ReadMode.CLUSTER:
        return "Mấy người đang hỏi/nói cùng chủ đề — đáp GỘP 1 lần, đừng lặp lại từng câu."
    return None   # SINGLE: không cần chỉ thị


def _room_reaction_prompt(dec) -> str:
    """SUMMARY/VIBE: chỉ thị react không khí chat (đường ambient, không user turn)."""
    if dec.read_mode == ReadMode.VIBE:
        return (
            "[Context — Mai react KHÔNG KHÍ chat, KHÔNG trả lời ai cụ thể]\n"
            "Chat đang bùng, cả đám spam cùng kiểu. React theo VIBE bằng 1 câu ngắn "
            "đúng giọng Mai, KHÔNG đáp lẻ từng người. Chỉ viết thoại."
        )
    return (
        "[Context — Mai react KHÔNG KHÍ chat, KHÔNG trả lời ai cụ thể]\n"
        "Chat trôi nhanh, nhiều tin lặt vặt đọc không kịp. Nói 1 câu tổng kiểu "
        "'chat trôi nhanh quá' đúng giọng Mai, KHÔNG đáp lẻ từng tin. Chỉ viết thoại."
    )


def _history_text_for(dec) -> tuple[str | None, bool]:
    """TASK 5: (history_user_text, commit_history) — text chat GỐC cho history/memory.

    SINGLE/ACK → text chat thật. CLUSTER → câu gọn ghép refs. SUMMARY/VIBE →
    (None, False): không có tin cụ thể, KHÔNG commit history/memory."""
    refs = dec.refs
    if dec.read_mode in (ReadMode.SUMMARY, ReadMode.VIBE) or not refs:
        return None, False
    if dec.read_mode == ReadMode.CLUSTER:
        joined = " / ".join(r.text for r in refs[:3])
        return f"(mấy người cùng hỏi) {joined}", True
    # SINGLE / ACK → text chat gốc
    return refs[0].text, True
