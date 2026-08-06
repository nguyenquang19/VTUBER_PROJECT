"""ChatPulse — đo độ sôi nổi chat (C0.2, ROADMAP §C0.3).

Nâng "đếm tin" thành tín hiệu năng lượng cho Director + mood + urge.
Sôi nổi ≠ chỉ số lượng — phải TÁCH:

    tempo     = tin/phút (rolling window)
    diversity = unique_users / msg_count      # hype-spam vs bàn luận thật
    accel     = tempo / baseline_tempo (EMA)  # >1 = đang bùng

| tempo | diversity | state      | Director (C0.3)                       |
|-------|-----------|------------|---------------------------------------|
| cao   | thấp      | HYPE_SPAM  | react VIBE, không đáp lẻ, turn ngắn   |
| cao   | cao       | LIVELY     | triage gắt, kéo top, đáp gọn          |
| thấp  | —         | COLD       | self_talk / đổi segment / gọi ông     |
| giữa  | —         | NORMAL     | đáp bình thường                       |

Rẻ: chỉ đếm + trung bình trượt, KHÔNG model. clock inject → test tất định.
"""
from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Any


class PulseState(str, Enum):
    COLD = "cold"
    HYPE_SPAM = "hype_spam"
    LIVELY = "lively"
    NORMAL = "normal"


class ChatPulse:
    def __init__(
        self,
        window_seconds: float = 60.0,
        tempo_low_per_min: float = 2.0,
        tempo_high_per_min: float = 15.0,
        diversity_threshold: float = 0.4,
        cold_silence_seconds: float = 90.0,
        baseline_alpha: float = 0.05,
        accel_hot_threshold: float = 1.5,
    ) -> None:
        self._window = max(1.0, float(window_seconds))
        self._tempo_low = float(tempo_low_per_min)
        self._tempo_high = float(tempo_high_per_min)
        self._div_thr = float(diversity_threshold)
        self._cold_silence = float(cold_silence_seconds)
        self._alpha = float(baseline_alpha)
        self._accel_hot = float(accel_hot_threshold)

        # deque of (ts, user_id)
        self._events: deque[tuple[float, str | None]] = deque()
        self._baseline_tempo: float | None = None
        self._last_ts: float | None = None

    @classmethod
    def from_loader(cls, loader) -> "ChatPulse":
        p = loader.get("chat_salience", "pulse", {}) or {}
        return cls(
            window_seconds=float(p.get("window_seconds", 60.0)),
            tempo_low_per_min=float(p.get("tempo_low_per_min", 2.0)),
            tempo_high_per_min=float(p.get("tempo_high_per_min", 15.0)),
            diversity_threshold=float(p.get("diversity_threshold", 0.4)),
            cold_silence_seconds=float(p.get("cold_silence_seconds", 90.0)),
            baseline_alpha=float(p.get("baseline_alpha", 0.05)),
            accel_hot_threshold=float(p.get("accel_hot_threshold", 1.5)),
        )

    # ---------- record ----------

    def record(self, now: float, user_id: str | None = None) -> None:
        self._events.append((now, user_id))
        self._last_ts = now
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    # ---------- metrics ----------

    def tempo(self, now: float) -> float:
        """Tin/phút trong window."""
        self._prune(now)
        return len(self._events) / (self._window / 60.0)

    def diversity(self, now: float) -> float:
        """unique_users / msg_count. 1.0 nếu không có tin (không coi là spam)."""
        self._prune(now)
        if not self._events:
            return 1.0
        users = {u for _, u in self._events if u is not None}
        # tin ẩn danh (user None) coi mỗi tin 1 người để không lệch về hype-spam giả
        anon = sum(1 for _, u in self._events if u is None)
        unique = len(users) + anon
        return unique / len(self._events)

    def accel(self, now: float) -> float:
        """tempo / baseline. 1.0 khi chưa có baseline."""
        t = self.tempo(now)
        if self._baseline_tempo is None or self._baseline_tempo <= 1e-9:
            return 1.0
        return t / self._baseline_tempo

    def seconds_since_last(self, now: float) -> float:
        """Giây từ tin cuối. inf nếu chưa có tin nào."""
        if self._last_ts is None:
            return float("inf")
        return now - self._last_ts

    def is_cold(self, now: float) -> bool:
        """Chat nguội: không tin trong cold_silence_seconds HOẶC tempo dưới ngưỡng thấp."""
        return self.seconds_since_last(now) >= self._cold_silence or self.tempo(now) < self._tempo_low

    # ---------- state ----------

    def update_baseline(self, now: float) -> None:
        """EMA baseline_tempo (Director tick gọi định kỳ cho accel). Không bắt buộc."""
        t = self.tempo(now)
        if self._baseline_tempo is None:
            self._baseline_tempo = t
        else:
            self._baseline_tempo = (1 - self._alpha) * self._baseline_tempo + self._alpha * t

    def state(self, now: float) -> PulseState:
        self._prune(now)
        tempo = self.tempo(now)
        if self.seconds_since_last(now) >= self._cold_silence or tempo < self._tempo_low:
            return PulseState.COLD
        if tempo >= self._tempo_high:
            return PulseState.HYPE_SPAM if self.diversity(now) < self._div_thr else PulseState.LIVELY
        return PulseState.NORMAL

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "pulse_state": self.state(now).value,
            "pulse_tempo_per_min": round(self.tempo(now), 2),
            "pulse_diversity": round(self.diversity(now), 3),
            "pulse_accel": round(self.accel(now), 2),
            "pulse_seconds_since_last": round(self.seconds_since_last(now), 1)
            if self._last_ts is not None else None,
        }
