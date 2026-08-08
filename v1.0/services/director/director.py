"""Director — đạo diễn stream, quyết định NÊN làm gì (C0.3, ROADMAP §C0.4).

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


class Director:
    def __init__(
        self,
        pool: SaliencePool,
        pulse: ChatPulse,
        segments: list[Segment],
        dead_air_seconds: float = 20.0,
        max_consecutive_read_chat: int = 3,
        max_refs_per_turn: int = 3,
        backlog_summary_threshold: int = 12,
        summary_score_ceiling: float = 15.0,
        ask_follow_up_before_expiry_s: float = 20.0,
        clock: Any = None,
    ) -> None:
        if not segments:
            raise ValueError("cần ít nhất 1 segment")
        self._pool = pool
        self._pulse = pulse
        self._segments = segments
        self._dead_air = float(dead_air_seconds)
        self._max_consec = max(1, int(max_consecutive_read_chat))
        self._max_refs = max(1, int(max_refs_per_turn))
        self._backlog_thr = int(backlog_summary_threshold)
        self._summary_ceiling = float(summary_score_ceiling)
        self._ask_follow_up_before_expiry_s = max(0.0, float(ask_follow_up_before_expiry_s))
        self._clock = clock or time.time

        self._seg_idx = 0
        self._seg_started_at: float | None = None
        self._last_speak_ts: float | None = None
        self._consecutive_read_chat = 0
        self._transitions = 0

    @classmethod
    def from_loader(cls, pool: SaliencePool, pulse: ChatPulse, loader, clock: Any = None) -> "Director":
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
            max_consecutive_read_chat=int(d.get("max_consecutive_read_chat", 3)),
            max_refs_per_turn=int(d.get("max_refs_per_turn", 3)),
            backlog_summary_threshold=int(d.get("backlog_summary_threshold", 12)),
            summary_score_ceiling=float(d.get("summary_score_ceiling", 15.0)),
            ask_follow_up_before_expiry_s=float(
                d.get("arbiter", {}).get("ask_follow_up_before_expiry_s", 20.0)
            ),
            clock=clock,
        )

    # ---------- lifecycle ----------

    def start(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._seg_idx = 0
        self._seg_started_at = now
        self._last_speak_ts = now

    @property
    def summary_ceiling(self) -> float:
        """Ngưỡng điểm cho SUMMARY — DirectorLoop dùng để purge backlog thấp (Task 3)."""
        return self._summary_ceiling

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
        if action == DirectorAction.READ_CHAT:
            self._consecutive_read_chat += 1
        else:
            self._consecutive_read_chat = 0

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

        if director_input.goals.active is not None:
            return self._goal_decision(seg, director_input.goals.active, director_input)

        # 1. Hết giờ segment → chuyển (Mai thông báo). Segment cuối không auto-chuyển.
        if (
            top is None
            and not self.is_last_segment()
            and (now - seg_started_at) >= seg.duration_seconds
        ):
            if "transition" in allowed:
                nxt = self._segments[self._seg_idx + 1]
                return DirectorDecision(
                    action=DirectorAction.TRANSITION, segment=seg.name,
                    reason="segment_time_elapsed", next_segment=nxt.name,
                )

        dead_air = float("inf") if self._last_speak_ts is None else now - self._last_speak_ts
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
        if pulse_state == PulseState.HYPE_SPAM and read_allowed and top is not None and not consec_hit:
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="hype_spam_vibe", refs=director_input.chat_candidates[:self._max_refs],
                read_mode=ReadMode.VIBE,
            )

        # 4. Có tin đáng đáp + chưa đáp chat liên tiếp quá nhiều → read_chat
        if read_allowed and top is not None and not consec_hit:
            return self._read_decision(seg, top, director_input)

        # 5. Chủ động: chat nguội / dead-air / bị ép xen / urge sẵn
        proactive_trigger = (
            pulse_state == PulseState.COLD
            or dead_air >= self._dead_air
            or consec_hit
            or director_input.urge_ready
        )
        if proactive_trigger:
            if director_input.agent_state.open_threads:
                return DirectorDecision(
                    action=DirectorAction.WAIT, segment=seg.name,
                    reason="open_thread_blocks_self_talk",
                )
            if "self_talk" in allowed:
                reason = (
                    "consec_read_chat_break" if consec_hit else
                    "cold_chat" if pulse_state == PulseState.COLD else
                    "dead_air" if dead_air >= self._dead_air else
                    "urge_ready"
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
        if read_allowed and top is not None:
            return self._read_decision(seg, top, director_input)

        return DirectorDecision(action=DirectorAction.WAIT, segment=seg.name, reason="idle")

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
