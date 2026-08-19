"""Director — đạo diễn stream, quyết định NÊN làm gì (C0.3; xem docs/MAI_V2_SYSTEM_SPEC.md).

Biến reactive (đáp mọi tin FIFO) → host: pure decision engine đọc
(segment, ChatPulse, SaliencePool top, dead-air, urge) → chốt 1 action.
KHÔNG gọi LLM mỗi tick — chỉ chốt action, caller mới sinh thoại.

Action:
  READ_CHAT    — đáp tin từ pool (read_mode: single/cluster/summary/vibe)
  ACK_DONATION — ưu tiên ack superchat (chen hàng)
  SELF_TALK    — Mai tự mở lời (dead-air / chat nguội / bị ép xen)
  FOLLOW_UP    — quay lại chủ đề trước (C1 mở rộng; MVP = self_talk biến thể)
  TRANSITION   — chuyển segment, Mai thông báo ("thôi chơi tiếp nào")
  WAIT         — chưa làm gì (chờ tick sau)

Quy tắc chống "máy đọc chat": max_refs_per_turn + đếm consecutive_read_chat →
sau N lần đáp chat liên tiếp ép xen chủ động (self_talk).

MVP: state machine + bảng action + rule if/else (❌ chưa utility-scoring nhiều chiều).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from services.director.chat_pulse import ChatPulse, PulseState
from services.director.salience import PooledMessage, SaliencePool
from services.director.action_types import DirectorChatRef, DirectorInput
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot
from services.agent.types import AgentStateSnapshot


class DirectorAction(str, Enum):
    READ_CHAT = "read_chat"
    ACK_DONATION = "ack_donation"
    SELF_TALK = "self_talk"
    FOLLOW_UP = "follow_up"
    CONTINUE_THREAD = "continue_thread"
    ASK_FOLLOW_UP = "ask_follow_up"
    SHARE_GOAL_PROGRESS = "share_goal_progress"
    TRANSITION = "transition"
    WAIT = "wait"


class ReadMode(str, Enum):
    SINGLE = "single"       # đáp thẳng top-1
    CLUSTER = "cluster"     # gộp cụm trùng, ref ≤ max_refs ("mấy cậu hỏi X hả")
    SUMMARY = "summary"     # backlog cao + điểm thấp → 1 câu tổng, không đáp lẻ
    VIBE = "vibe"           # hype-spam → react vibe, turn ngắn, không đáp lẻ
    ACK = "ack"             # ack donation riêng


@dataclass
class Segment:
    name: str
    goal: str
    duration_seconds: float
    allowed_actions: set[str]


@dataclass(frozen=True)
class DirectorDecision:
    action: DirectorAction
    segment: str
    reason: str
    refs: tuple[DirectorChatRef, ...] = ()
    read_mode: ReadMode | None = None
    next_segment: str | None = None   # khi action=TRANSITION
    goal_id: str | None = None
    proactive_source: str | None = None
    proactive_source_id: str | None = None
    proactive_category: str | None = None
    proactive_evidence_ids: tuple[str, ...] = ()
    proactive_summary: str = ""


class Director:
    def __init__(
        self,
        pool: SaliencePool,
        pulse: ChatPulse,
        segments: list[Segment],
        dead_air_seconds: float = 20.0,
        self_talk_cooldown_seconds: float = 45.0,
        room_reaction_cooldown_seconds: float = 30.0,
        max_consecutive_read_chat: int = 3,
        max_refs_per_turn: int = 3,
        backlog_summary_threshold: int = 12,
        summary_score_ceiling: float = 15.0,
        min_actionable_score: float = 15.0,
        chat_gate_enabled: bool = False,
        ask_follow_up_before_expiry_s: float = 20.0,
        mood_policy: Any = None,
        proactive_policy: Any = None,
        clock: Any = None,
    ) -> None:
        if not segments:
            raise ValueError("cần ít nhất 1 segment")
        self._pool = pool
        self._pulse = pulse
        self._segments = segments
        self._dead_air = float(dead_air_seconds)
        self._self_talk_cooldown = max(0.0, float(self_talk_cooldown_seconds))
        self._room_reaction_cooldown = max(
            0.0, float(room_reaction_cooldown_seconds),
        )
        self._max_consec = max(1, int(max_consecutive_read_chat))
        self._max_refs = max(1, int(max_refs_per_turn))
        self._backlog_thr = int(backlog_summary_threshold)
        self._summary_ceiling = float(summary_score_ceiling)
        self._min_actionable_score = max(0.0, float(min_actionable_score))
        self._chat_gate_enabled = bool(chat_gate_enabled)
        self._ask_follow_up_before_expiry_s = max(0.0, float(ask_follow_up_before_expiry_s))
        self._mood_policy = mood_policy
        self._proactive_policy = proactive_policy
        self._clock = clock or time.time

        self._seg_idx = 0
        self._seg_started_at: float | None = None
        self._last_speak_ts: float | None = None
        self._last_self_talk_ts: float | None = None
        self._self_talk_deferred_until = 0.0
        self._last_room_reaction_ts: float | None = None
        self._room_reaction_deferred_until = 0.0
        self._consecutive_read_chat = 0
        self._transitions = 0

    @classmethod
    def from_loader(
        cls, pool: SaliencePool, pulse: ChatPulse, loader, clock: Any = None,
        mood_policy: Any = None,
        proactive_policy: Any = None,
        chat_gate_enabled: bool = True,
    ) -> "Director":
        d = loader.get("director", "director", {}) or {}
        segs_raw = d.get("segments", []) or []
        segments = [
            Segment(
                name=str(s.get("name", f"seg{i}")),
                goal=str(s.get("goal", "")),
                duration_seconds=float(s.get("duration_seconds", 300)),
                allowed_actions=set(s.get("allowed_actions", [])),
            )
            for i, s in enumerate(segs_raw)
        ]
        if not segments:  # fail-safe: 1 segment mặc định
            segments = [Segment("main", "nội dung", 1800, {"read_chat", "self_talk", "transition"})]
        return cls(
            pool=pool, pulse=pulse, segments=segments,
            dead_air_seconds=float(d.get("dead_air_seconds", 20.0)),
            self_talk_cooldown_seconds=float(
                d.get("self_talk_cooldown_seconds", 45.0)
            ),
            room_reaction_cooldown_seconds=float(
                (d.get("room_reaction") or {}).get("cooldown_seconds", 30.0)
            ),
            max_consecutive_read_chat=int(d.get("max_consecutive_read_chat", 3)),
            max_refs_per_turn=int(d.get("max_refs_per_turn", 3)),
            backlog_summary_threshold=int(d.get("backlog_summary_threshold", 12)),
            summary_score_ceiling=float(d.get("summary_score_ceiling", 15.0)),
            min_actionable_score=float(d.get("min_actionable_score", 15.0)),
            chat_gate_enabled=chat_gate_enabled,
            ask_follow_up_before_expiry_s=float(
                d.get("arbiter", {}).get("ask_follow_up_before_expiry_s", 20.0)
            ),
            mood_policy=mood_policy,
            proactive_policy=proactive_policy,
            clock=clock,
        )

    # ---------- lifecycle ----------

    def start(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._seg_idx = 0
        self._seg_started_at = now
        self._last_speak_ts = now
        self._last_self_talk_ts = None
        self._self_talk_deferred_until = 0.0
        self._last_room_reaction_ts = None
        self._room_reaction_deferred_until = 0.0

    @property
    def summary_ceiling(self) -> float:
        """Ngưỡng điểm cho SUMMARY — DirectorLoop dùng để purge backlog thấp (Task 3)."""
        return self._summary_ceiling

    @property
    def chat_gate_enabled(self) -> bool:
        return self._chat_gate_enabled

    def set_chat_gate_enabled(self, enabled: bool) -> None:
        """Runtime toggle; OFF khôi phục việc đọc mọi candidate trên storage floor."""
        self._chat_gate_enabled = bool(enabled)

    def current_segment(self) -> Segment:
        return self._segments[self._seg_idx]

    def is_last_segment(self) -> bool:
        return self._seg_idx >= len(self._segments) - 1

    def advance_segment(self, now: float | None = None) -> Segment:
        """Chuyển segment kế (caller gọi sau khi Mai đã nói câu transition)."""
        now = self._clock() if now is None else now
        if not self.is_last_segment():
            self._seg_idx += 1
            self._transitions += 1
        self._seg_started_at = now
        return self.current_segment()

    def mark_spoke(self, action: DirectorAction, now: float | None = None) -> None:
        """Caller gọi sau khi Mai nói xong 1 turn. Cập nhật dead-air + đếm read_chat."""
        now = self._clock() if now is None else now
        self._last_speak_ts = now
        if action == DirectorAction.SELF_TALK:
            self._last_self_talk_ts = now
        if action == DirectorAction.READ_CHAT:
            self._consecutive_read_chat += 1
        else:
            self._consecutive_read_chat = 0

    def defer_self_talk(self, until: float) -> None:
        """Back off failed/no-material ambient attempts without faking speech."""
        self._self_talk_deferred_until = max(
            self._self_talk_deferred_until, float(until),
        )

    def clear_self_talk_defer(self) -> None:
        """New external evidence may make a fresh thought possible immediately."""
        self._self_talk_deferred_until = 0.0

    def mark_room_reaction(self, now: float | None = None) -> None:
        """Start room cooldown only after a SUMMARY/VIBE reached delivery."""
        self._last_room_reaction_ts = self._clock() if now is None else float(now)
        self._room_reaction_deferred_until = 0.0

    def defer_room_reaction(self, until: float) -> None:
        """Back off a repeated room candidate without faking delivered speech."""
        self._room_reaction_deferred_until = max(
            self._room_reaction_deferred_until, float(until),
        )

    def room_reaction_ready(self, now: float) -> bool:
        """Return whether scheduling may open another generic room reaction."""
        cooldown_ready = (
            self._last_room_reaction_ts is None
            or float(now) - self._last_room_reaction_ts >= self._room_reaction_cooldown
        )
        return cooldown_ready and float(now) >= self._room_reaction_deferred_until

    # ---------- decision ----------

    def decide(
        self,
        director_input: DirectorInput | None = None,
        *,
        now: float | None = None,
        urge_ready: bool = False,
    ) -> DirectorDecision:
        """Return one deterministic decision without mutating supplied snapshots."""
        if director_input is None:
            director_input = self._legacy_input(now, urge_ready)
        now = director_input.now
        seg = self.current_segment()
        allowed = seg.allowed_actions
        seg_started_at = now if self._seg_started_at is None else self._seg_started_at
        top = director_input.chat_candidates[0] if director_input.chat_candidates else None
        top_actionable = self._is_actionable(top)

        if director_input.safety_hold:
            return DirectorDecision(
                action=DirectorAction.WAIT, segment=seg.name, reason="safety_hold",
            )

        donation = next((ref for ref in director_input.chat_candidates if ref.is_super), None)
        if donation is not None and "ack_donation" in allowed:
            return DirectorDecision(
                action=DirectorAction.ACK_DONATION, segment=seg.name,
                reason="superchat_priority", refs=(donation,), read_mode=ReadMode.ACK,
                goal_id=_matching_donation_goal_id(director_input.goals.active, donation),
            )

        active_goal = director_input.goals.active
        continue_source_pending = bool(
            active_goal is not None
            and active_goal.kind is GoalKind.CONTINUE_THREAD
            and active_goal.metadata.get("source_delivered") is False
        )
        if active_goal is not None and not continue_source_pending:
            return self._goal_decision(seg, active_goal, director_input)

        # 1. Hết giờ segment → chuyển sau hard priority/goal. Backlog không được
        # giữ một segment vô hạn; chat vẫn còn nguyên trong pool sau transition.
        if (
            not self.is_last_segment()
            and (now - seg_started_at) >= seg.duration_seconds
        ):
            if "transition" in allowed:
                nxt = self._segments[self._seg_idx + 1]
                return DirectorDecision(
                    action=DirectorAction.TRANSITION, segment=seg.name,
                    reason="segment_time_elapsed", next_segment=nxt.name,
                )

        dead_air = float("inf") if self._last_speak_ts is None else now - self._last_speak_ts
        self_talk_ready = (
            self._last_self_talk_ts is None
            or now - self._last_self_talk_ts >= self._self_talk_cooldown
        ) and now >= self._self_talk_deferred_until and director_input.self_talk_ready
        try:
            pulse_state = PulseState(director_input.pulse_state)
        except ValueError:
            pulse_state = PulseState.NORMAL
        read_allowed = "read_chat" in allowed
        consec_hit = self._consecutive_read_chat >= self._max_consec

        # 2. Superchat chen hàng — ack ngay (nếu segment cho phép)
        if top is not None and top.is_super and "ack_donation" in allowed:
            return DirectorDecision(
                action=DirectorAction.ACK_DONATION, segment=seg.name,
                reason="superchat_priority", refs=(top,), read_mode=ReadMode.ACK,
            )

        # 3. Hype-spam → react vibe (turn ngắn, không đáp lẻ 30 câu)
        if (
            pulse_state == PulseState.HYPE_SPAM
            and read_allowed
            and top is not None
            and top_actionable
            and not consec_hit
            and top.kind == "chat"
        ):
            if not self.room_reaction_ready(now):
                return DirectorDecision(
                    action=DirectorAction.WAIT, segment=seg.name,
                    reason="room_reaction_cooldown",
                )
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="hype_spam_vibe", refs=director_input.chat_candidates[:self._max_refs],
                read_mode=ReadMode.VIBE,
            )

        # 4. Có tin đáng đáp + chưa đáp chat liên tiếp quá nhiều → read_chat
        if read_allowed and top is not None and top_actionable and not consec_hit:
            return self._read_decision(seg, top, director_input)

        # Backlog thấp điểm chỉ tạo một room summary, không biến từng chat thường
        # thành một turn riêng.
        if (
            read_allowed
            and top is not None
            and not top_actionable
            and not consec_hit
            and director_input.pool_size >= self._backlog_thr
            and top.score < self._summary_ceiling
        ):
            return self._read_decision(seg, top, director_input)

        # 5. Chủ động: chat nguội / dead-air / bị ép xen / urge sẵn
        silence_ready = dead_air >= self._dead_air and self_talk_ready
        mood_proactive_ready = self._mood_proactive_ready(director_input)
        proactive_trigger = (
            silence_ready
            or (
                consec_hit
                and director_input.self_talk_ready
                and now >= self._self_talk_deferred_until
            )
            or (director_input.urge_ready and self_talk_ready)
            or (mood_proactive_ready and self_talk_ready)
        )
        proactive = self._proactive_choice(
            director_input, allowed, silence_ready=silence_ready,
        )
        if proactive is not None:
            return DirectorDecision(
                action=proactive.action, segment=seg.name, reason=proactive.reason,
                proactive_source=proactive.source.value,
                proactive_source_id=proactive.source_id,
                proactive_category=proactive.category,
                proactive_evidence_ids=proactive.evidence_ids,
                proactive_summary=proactive.summary,
            )
        if self._proactive_policy is None and director_input.agent_state.open_threads:
            return DirectorDecision(
                action=DirectorAction.WAIT, segment=seg.name,
                reason="open_thread_blocks_self_talk",
            )
        if proactive_trigger:
            if "self_talk" in allowed:
                reason = (
                    "consec_read_chat_break" if consec_hit else
                    "cold_chat" if silence_ready and pulse_state == PulseState.COLD else
                    "dead_air" if silence_ready else
                    "urge_ready" if director_input.urge_ready else
                    "mood_action_score"
                )
                return DirectorDecision(
                    action=DirectorAction.SELF_TALK, segment=seg.name, reason=reason,
                )
            # segment không cho self_talk nhưng cho transition (VD closing) → chuyển
            if "transition" in allowed and not self.is_last_segment():
                nxt = self._segments[self._seg_idx + 1]
                return DirectorDecision(
                    action=DirectorAction.TRANSITION, segment=seg.name,
                    reason="proactive_no_selftalk", next_segment=nxt.name,
                )

        # 6. Bị ép xen nhưng không self_talk được, vẫn còn tin → thà đáp còn hơn im
        if read_allowed and top is not None and top_actionable:
            return self._read_decision(seg, top, director_input)

        if read_allowed and top is not None and not top_actionable:
            return DirectorDecision(
                action=DirectorAction.WAIT,
                segment=seg.name,
                reason="below_actionable_score",
            )

        if (
            "self_talk" in allowed and dead_air >= self._dead_air
            and not self_talk_ready
        ):
            return DirectorDecision(
                action=DirectorAction.WAIT,
                segment=seg.name,
                reason=(
                    "thought_unavailable"
                    if now < self._self_talk_deferred_until
                    else director_input.self_talk_wait_reason
                    if not director_input.self_talk_ready
                    else "self_talk_cooldown"
                ),
            )

        return DirectorDecision(action=DirectorAction.WAIT, segment=seg.name, reason="idle")

    def _is_actionable(self, top: DirectorChatRef | None) -> bool:
        if top is None:
            return False
        if not self._chat_gate_enabled:
            return True
        if top.is_super:
            return True
        return top.score >= self._min_actionable_score

    def _proactive_choice(
        self, value: DirectorInput, allowed: set[str], *, silence_ready: bool,
    ) -> Any:
        if self._proactive_policy is None:
            return None
        try:
            return self._proactive_policy.choose(
                value, allowed_actions=allowed, silence_ready=silence_ready,
            )
        except Exception:
            return None

    def mark_proactive_used(self, decision: DirectorDecision, now: float) -> None:
        if (
            self._proactive_policy is None or not decision.proactive_source
            or not decision.proactive_source_id
        ):
            return
        try:
            self._proactive_policy.mark_source_used(
                decision.proactive_source, decision.proactive_source_id, now,
            )
        except Exception:
            pass

    def _mood_proactive_ready(self, value: DirectorInput) -> bool:
        if self._mood_policy is None:
            return False
        try:
            return bool(self._mood_policy.proactive_ready(value.mood, value.tone_flags))
        except Exception:
            return False

    def _goal_decision(
        self, seg: Segment, goal: Goal, director_input: DirectorInput,
    ) -> DirectorDecision:
        """Map the active grounded goal to one bounded action or an explicit wait."""
        allowed = seg.allowed_actions
        goal_id = goal.goal_id
        if goal.kind is GoalKind.ACK_DONATION:
            return DirectorDecision(
                DirectorAction.WAIT, seg.name, "donation_evidence_missing", goal_id=goal_id,
            )
        if goal.kind is GoalKind.ANSWER_FOLLOW_UP:
            event_id = str(goal.metadata.get("chat_event_id") or "")
            ref = next(
                (item for item in director_input.chat_candidates if _same_event(item.msg_id, event_id)),
                None,
            )
            if ref is None:
                return DirectorDecision(
                    DirectorAction.WAIT, seg.name, "follow_up_evidence_missing", goal_id=goal_id,
                )
            if "read_chat" not in allowed:
                return DirectorDecision(
                    DirectorAction.WAIT, seg.name, "goal_action_not_allowed", goal_id=goal_id,
                )
            return DirectorDecision(
                DirectorAction.READ_CHAT, seg.name, "answer_follow_up_goal",
                refs=(ref,), read_mode=ReadMode.SINGLE, goal_id=goal_id,
            )
        if goal.kind is GoalKind.CONTINUE_THREAD:
            thread_exists = any(
                thread.thread_id == goal.parent_thread_id
                for thread in director_input.agent_state.open_threads
            )
            if thread_exists and "continue_thread" in allowed:
                return DirectorDecision(
                    DirectorAction.CONTINUE_THREAD, seg.name, "continue_active_thread",
                    goal_id=goal_id,
                )
            return DirectorDecision(
                DirectorAction.WAIT, seg.name,
                "thread_missing" if not thread_exists else "goal_action_not_allowed",
                goal_id=goal_id,
            )
        if goal.kind is GoalKind.WAIT_FOR_CHAT_ANSWER:
            remaining = goal.expires_at.timestamp() - director_input.now
            if (
                not goal.metadata.get("follow_up_asked")
                and remaining <= self._ask_follow_up_before_expiry_s
                and "ask_follow_up" in allowed
            ):
                return DirectorDecision(
                    DirectorAction.ASK_FOLLOW_UP, seg.name, "waiting_goal_near_expiry",
                    goal_id=goal_id,
                )
            return DirectorDecision(
                DirectorAction.WAIT, seg.name, "waiting_for_chat_answer", goal_id=goal_id,
            )
        if goal.kind is GoalKind.OPERATOR_PINNED:
            if "share_goal_progress" in allowed and not goal.metadata.get("progress_shared"):
                return DirectorDecision(
                    DirectorAction.SHARE_GOAL_PROGRESS, seg.name, "operator_goal_progress",
                    goal_id=goal_id,
                )
            return DirectorDecision(
                DirectorAction.WAIT, seg.name, "operator_goal_pending", goal_id=goal_id,
            )
        return DirectorDecision(
            DirectorAction.WAIT, seg.name, "unsupported_goal_kind", goal_id=goal_id,
        )

    def _read_decision(
        self, seg: Segment, top: DirectorChatRef, director_input: DirectorInput,
    ) -> DirectorDecision:
        """Chọn read_mode: summary (backlog cao+điểm thấp) / cluster / single."""
        pool_size = director_input.pool_size
        top_score = top.score
        if pool_size >= self._backlog_thr and top_score < self._summary_ceiling:
            if not self.room_reaction_ready(director_input.now):
                return DirectorDecision(
                    action=DirectorAction.WAIT, segment=seg.name,
                    reason="room_reaction_cooldown",
                )
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="backlog_summary", refs=(), read_mode=ReadMode.SUMMARY,
            )
        if top.cluster_count > 1:
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="cluster_merge", refs=director_input.chat_candidates[:self._max_refs],
                read_mode=ReadMode.CLUSTER,
            )
        return DirectorDecision(
            action=DirectorAction.READ_CHAT, segment=seg.name,
            reason="top_single", refs=(top,), read_mode=ReadMode.SINGLE,
        )

    def _legacy_input(self, now: float | None, urge_ready: bool) -> DirectorInput:
        current = self._clock() if now is None else now
        refs = tuple(
            _chat_ref(item, self._pool.current_score(item, current))
            for item in self._pool.top_cluster(current, self._max_refs)
        )
        return DirectorInput(
            now=current,
            agent_state=AgentStateSnapshot(),
            goals=GoalSnapshot(),
            chat_candidates=refs,
            pool_size=self._pool.size(),
            pulse_state=self._pulse.state(current).value,
            urge_ready=urge_ready,
        )

    # ---------- introspection ----------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "director_segment": self.current_segment().name,
            "director_segment_idx": self._seg_idx,
            "director_transitions": self._transitions,
            "director_consecutive_read_chat": self._consecutive_read_chat,
            "director_chat_gate_enabled": self._chat_gate_enabled,
            "director_min_actionable_score": self._min_actionable_score,
            "director_self_talk_cooldown_seconds": self._self_talk_cooldown,
            "director_last_self_talk_ts": self._last_self_talk_ts,
            "director_self_talk_deferred_until": self._self_talk_deferred_until,
            "director_room_reaction_cooldown_seconds": self._room_reaction_cooldown,
            "director_last_room_reaction_ts": self._last_room_reaction_ts,
            "director_room_reaction_deferred_until": self._room_reaction_deferred_until,
        }


def _chat_ref(message: PooledMessage, score: float) -> DirectorChatRef:
    return DirectorChatRef(
        msg_id=message.msg_id,
        text=message.text,
        kind=message.kind,
        score=score,
        created_at=message.created_at,
        viewer_id=message.viewer_id,
        viewer_name=message.viewer_name,
        amount_vnd=message.amount_vnd,
        is_super=message.is_super,
        cluster_count=message.cluster_count,
    )


def _same_event(message_id: str, event_id: str) -> bool:
    if not event_id:
        return False
    return message_id == event_id or event_id.endswith(f":{message_id}")


def _matching_donation_goal_id(goal: Goal | None, ref: DirectorChatRef) -> str | None:
    if goal is None or goal.kind is not GoalKind.ACK_DONATION:
        return None
    source_event_id = str(goal.metadata.get("source_event_id") or "")
    return goal.goal_id if not source_event_id or _same_event(ref.msg_id, source_event_id) else None
