"""Response pacing + filler (A3; xem docs/MAI_V2_SYSTEM_SPEC.md).

Hai thành phần độc lập, pure-logic (RNG inject → test tất định):

- `ResponsePacer.delay(text)`: thời gian chờ TRƯỚC khi Mai nói, biến thiên theo
  câu. base + scale-độ-dài + bonus-độ-khó + noise gauss, clamp [min,max].
  Mục tiêu: phá nhịp ~đều nhau (thứ lộ AI rõ nhất). σ>0 → mỗi lượt khác nhau.

- `FillerManager.maybe_pick(now)`: có nên chèn filler ("ừm"/"à") lượt này không,
  và clip nào. Gate: probability + frequency_cap/phút + cooldown + no-repeat.
  Trả clip path hoặc None. Pool rỗng → luôn None (no-op tới khi user thu clip).

KHÔNG tự phát audio ở đây — chỉ QUYẾT ĐỊNH. Caller (stream_runtime) load clip +
enqueue vào AudioPlayer. Tách để test decision không cần device/asset.
"""
from __future__ import annotations

import random
from collections import deque


class ResponsePacer:
    def __init__(
        self,
        base_seconds: float = 0.25,
        per_char_seconds: float = 0.004,
        question_bonus_seconds: float = 0.2,
        sigma_seconds: float = 0.15,
        min_seconds: float = 0.15,
        max_seconds: float = 1.4,
        enabled: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self.base = base_seconds
        self.per_char = per_char_seconds
        self.question_bonus = question_bonus_seconds
        self.sigma = max(0.0, sigma_seconds)
        self.min_s = min_seconds
        self.max_s = max_seconds
        self.enabled = enabled
        self._rng = rng or random.Random()

    @classmethod
    def from_loader(cls, loader, rng: random.Random | None = None) -> "ResponsePacer":
        c = loader.get("pacing", "response_delay", {}) or {}
        return cls(
            base_seconds=float(c.get("base_seconds", 0.25)),
            per_char_seconds=float(c.get("per_char_seconds", 0.004)),
            question_bonus_seconds=float(c.get("question_bonus_seconds", 0.2)),
            sigma_seconds=float(c.get("sigma_seconds", 0.15)),
            min_seconds=float(c.get("min_seconds", 0.15)),
            max_seconds=float(c.get("max_seconds", 1.4)),
            enabled=bool(c.get("enabled", True)),
            rng=rng,
        )

    def delay(self, text: str) -> float:
        """Tính delay (giây) cho câu `text`. enabled=False → 0."""
        if not self.enabled:
            return 0.0
        n = len(text or "")
        d = self.base + n * self.per_char
        # proxy độ khó rẻ: câu hỏi thường cần "nghĩ" hơn
        if "?" in (text or ""):
            d += self.question_bonus
        if self.sigma > 0:
            d += self._rng.gauss(0.0, self.sigma)
        # clamp
        return max(self.min_s, min(self.max_s, d))


class FillerManager:
    def __init__(
        self,
        clips: list[str] | None = None,
        probability: float = 0.35,
        frequency_cap_per_min: int = 4,
        cooldown_seconds: float = 6.0,
        no_repeat_last_n: int = 2,
        enabled: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._clips = [c for c in (clips or []) if c]
        self.probability = probability
        self.cap_per_min = max(0, frequency_cap_per_min)
        self.cooldown = cooldown_seconds
        self._no_repeat = max(0, no_repeat_last_n)
        self.enabled = enabled
        self._rng = rng or random.Random()

        self._recent: deque[str] = deque(maxlen=self._no_repeat)
        self._play_times: deque[float] = deque()   # timestamps trong 60s gần nhất
        self._last_play_ts: float | None = None

        # metrics (P2 observability)
        self.played = 0
        self.suppressed_cooldown = 0
        self.suppressed_cap = 0
        self.suppressed_prob = 0

    @classmethod
    def from_loader(cls, loader, rng: random.Random | None = None) -> "FillerManager":
        c = loader.get("pacing", "filler", {}) or {}
        return cls(
            clips=list(c.get("clips", []) or []),
            probability=float(c.get("probability", 0.35)),
            frequency_cap_per_min=int(c.get("frequency_cap_per_min", 4)),
            cooldown_seconds=float(c.get("cooldown_seconds", 6.0)),
            no_repeat_last_n=int(c.get("no_repeat_last_n", 2)),
            enabled=bool(c.get("enabled", True)),
            rng=rng,
        )

    def maybe_pick(self, now: float) -> str | None:
        """Trả clip path nên phát lượt này, hoặc None. `now` = time.time() (giây)."""
        if not self.enabled or not self._clips:
            return None

        # cooldown: quá gần lần trước → bỏ
        if self._last_play_ts is not None and (now - self._last_play_ts) < self.cooldown:
            self.suppressed_cooldown += 1
            return None

        # frequency cap: xén window 60s rồi đếm
        cutoff = now - 60.0
        while self._play_times and self._play_times[0] < cutoff:
            self._play_times.popleft()
        if self.cap_per_min > 0 and len(self._play_times) >= self.cap_per_min:
            self.suppressed_cap += 1
            return None

        # probability gate
        if self._rng.random() >= self.probability:
            self.suppressed_prob += 1
            return None

        # chọn clip không lặp trong window
        candidates = [c for c in self._clips if c not in self._recent]
        if not candidates:
            candidates = list(self._clips)  # pool nhỏ hơn window → cho lặp còn hơn im
        pick = self._rng.choice(candidates)

        self._recent.append(pick)
        self._play_times.append(now)
        self._last_play_ts = now
        self.played += 1
        return pick

    def get_metrics(self) -> dict[str, int]:
        return {
            "filler_played": self.played,
            "filler_suppressed_cooldown": self.suppressed_cooldown,
            "filler_suppressed_cap": self.suppressed_cap,
            "filler_suppressed_prob": self.suppressed_prob,
        }
