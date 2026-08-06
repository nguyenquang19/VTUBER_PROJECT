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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from services.director.chat_pulse import ChatPulse, PulseState
from services.director.salience import PooledMessage, SaliencePool


class DirectorAction(str, Enum):
    READ_CHAT = "read_chat"
    ACK_DONATION = "ack_donation"
    SELF_TALK = "self_talk"
    FOLLOW_UP = "follow_up"
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


@dataclass
class DirectorDecision:
    action: DirectorAction
    segment: str
    reason: str
    refs: list[PooledMessage] = field(default_factory=list)
    read_mode: ReadMode | None = None
    next_segment: str | None = None   # khi action=TRANSITION


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

    def decide(self, now: float | None = None, urge_ready: bool = False) -> DirectorDecision:
        now = self._clock() if now is None else now
        if self._seg_started_at is None:
            self.start(now)
        seg = self.current_segment()
        allowed = seg.allowed_actions

        # 1. Hết giờ segment → chuyển (Mai thông báo). Segment cuối không auto-chuyển.
        if not self.is_last_segment() and (now - self._seg_started_at) >= seg.duration_seconds:
            if "transition" in allowed:
                nxt = self._segments[self._seg_idx + 1]
                return DirectorDecision(
                    action=DirectorAction.TRANSITION, segment=seg.name,
                    reason="segment_time_elapsed", next_segment=nxt.name,
                )

        top = self._pool.peek_top(now)
        dead_air = float("inf") if self._last_speak_ts is None else now - self._last_speak_ts
        pulse_state = self._pulse.state(now)
        read_allowed = "read_chat" in allowed
        consec_hit = self._consecutive_read_chat >= self._max_consec

        # 2. Superchat chen hàng — ack ngay (nếu segment cho phép)
        if top is not None and top.is_super and "ack_donation" in allowed:
            return DirectorDecision(
                action=DirectorAction.ACK_DONATION, segment=seg.name,
                reason="superchat_priority", refs=[top], read_mode=ReadMode.ACK,
            )

        # 3. Hype-spam → react vibe (turn ngắn, không đáp lẻ 30 câu)
        if pulse_state == PulseState.HYPE_SPAM and read_allowed and top is not None and not consec_hit:
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="hype_spam_vibe", refs=self._pool.top_cluster(now, self._max_refs),
                read_mode=ReadMode.VIBE,
            )

        # 4. Có tin đáng đáp + chưa đáp chat liên tiếp quá nhiều → read_chat
        if read_allowed and top is not None and not consec_hit:
            return self._read_decision(seg, top, now)

        # 5. Chủ động: chat nguội / dead-air / bị ép xen / urge sẵn
        proactive_trigger = (
            pulse_state == PulseState.COLD
            or dead_air >= self._dead_air
            or consec_hit
            or urge_ready
        )
        if proactive_trigger:
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
            return self._read_decision(seg, top, now)

        return DirectorDecision(action=DirectorAction.WAIT, segment=seg.name, reason="idle")

    def _read_decision(self, seg: Segment, top: PooledMessage, now: float) -> DirectorDecision:
        """Chọn read_mode: summary (backlog cao+điểm thấp) / cluster / single."""
        pool_size = self._pool.size()
        top_score = self._pool.current_score(top, now)
        if pool_size >= self._backlog_thr and top_score < self._summary_ceiling:
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="backlog_summary", refs=[], read_mode=ReadMode.SUMMARY,
            )
        if top.cluster_count > 1:
            return DirectorDecision(
                action=DirectorAction.READ_CHAT, segment=seg.name,
                reason="cluster_merge", refs=self._pool.top_cluster(now, self._max_refs),
                read_mode=ReadMode.CLUSTER,
            )
        return DirectorDecision(
            action=DirectorAction.READ_CHAT, segment=seg.name,
            reason="top_single", refs=[top], read_mode=ReadMode.SINGLE,
        )

    # ---------- introspection ----------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "director_segment": self.current_segment().name,
            "director_segment_idx": self._seg_idx,
            "director_transitions": self._transitions,
            "director_consecutive_read_chat": self._consecutive_read_chat,
        }
