"""AutonomyEngine v2 core: UrgeAccumulator + CategorySelector (Aut.A).

Spec: docs/AUTONOMY_ENGINE_REDESIGN.md — thay hard `silence > 60s` bằng urge
accumulator probabilistic + category selector có mood coupling + no-repeat.

Đây là 2 building block core, chưa gồm material pipeline (Aut.B) hay composer
(Aut.C). Sync API (không async — tick chạy trong bg loop ngoài).

Design chốt v2 (fix 5 vấn đề bản gốc):
1. Threshold không hằng số → prob curve + Gaussian noise mỗi tick
2. Nhiều lý do nói (5 category) → CategorySelector
3. Self-cooldown TÁCH khỏi last_speak_time (2 biến, không dùng chung)
4. Mood coupling: bon_chon boost, buon/nguong dampen
5. Nag decay: consecutive_ignored → nag_penalty (0.4-1.0)
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from interfaces.animation import MoodState
from orchestrator.logger import get_logger


# ─────────────────────── Config dataclasses ───────────────────────


@dataclass
class CategoryConfig:
    name: str
    weight: float = 1.0
    cooldown_seconds: int = 300
    mood_boost: dict[str, float] = field(default_factory=dict)  # {dim: mult}
    prompt_hint: str = ""


@dataclass
class UrgeConfig:
    rise_base: float = 0.5
    rise_max_per_tick: float = 12.0
    urge_floor: float = 30.0
    urge_noise_std: float = 3.0
    decay_after_speak: float = 8.0
    self_cooldown_seconds: int = 45
    bon_chon_weight: float = 0.6
    buon_dampen: float = 0.4
    nguong_dampen: float = 0.5
    prob_scale: float = 40.0
    prob_max: float = 0.85


@dataclass
class AutonomyConfig:
    tick_seconds: float = 5.0
    urge: UrgeConfig = field(default_factory=UrgeConfig)
    no_repeat_window: int = 2
    categories: dict[str, CategoryConfig] = field(default_factory=dict)

    @classmethod
    def from_loader(cls, loader) -> "AutonomyConfig":
        raw = loader.get("autonomy", "autonomy", {}) or {}
        urge_raw = raw.get("urge", {}) or {}
        cats_raw = raw.get("categories", {}) or {}
        cats = {
            name: CategoryConfig(
                name=name,
                weight=float(c.get("weight", 1.0)),
                cooldown_seconds=int(c.get("cooldown_seconds", 300)),
                mood_boost={k: float(v) for k, v in (c.get("mood_boost") or {}).items()},
                prompt_hint=str(c.get("prompt_hint", "")),
            )
            for name, c in cats_raw.items()
        }
        return cls(
            tick_seconds=float(raw.get("tick_seconds", 5.0)),
            urge=UrgeConfig(
                rise_base=float(urge_raw.get("rise_base", 0.5)),
                rise_max_per_tick=float(urge_raw.get("rise_max_per_tick", 12.0)),
                urge_floor=float(urge_raw.get("urge_floor", 30.0)),
                urge_noise_std=float(urge_raw.get("urge_noise_std", 3.0)),
                decay_after_speak=float(urge_raw.get("decay_after_speak", 8.0)),
                self_cooldown_seconds=int(urge_raw.get("self_cooldown_seconds", 45)),
                bon_chon_weight=float(urge_raw.get("bon_chon_weight", 0.6)),
                buon_dampen=float(urge_raw.get("buon_dampen", 0.4)),
                nguong_dampen=float(urge_raw.get("nguong_dampen", 0.5)),
                prob_scale=float(urge_raw.get("prob_scale", 40.0)),
                prob_max=float(urge_raw.get("prob_max", 0.85)),
            ),
            no_repeat_window=int(raw.get("no_repeat_window", 2)),
            categories=cats,
        )


# ─────────────────────── UrgeAccumulator ───────────────────────


class UrgeAccumulator:
    """Tích urge score 0-100, tick liên tục thay vì check-on-demand.

    `last_external_activity` = khi chat/operator nói (reset qua on_external_activity)
    `last_self_speak` = khi Mai tự nói (reset qua on_self_spoke, khác cái trên)
    """

    def __init__(
        self,
        cfg: UrgeConfig,
        clock=None,          # inject cho test deterministic
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock or time.time
        self._rng = rng or random.Random()

        self.urge: float = 0.0
        now = self._clock()
        self.last_external_activity_ts: float = now
        self.last_self_speak_ts: float = now - cfg.self_cooldown_seconds  # ngoài cooldown ngay lúc start
        self.consecutive_ignored: int = 0

        self._ticks: int = 0
        self._speak_decisions: int = 0
        self._log = get_logger("autonomy.urge")

    def tick(self, current_mood: MoodState) -> None:
        """1 tick: update urge dựa silence + mood + noise + nag."""
        self._ticks += 1
        now = self._clock()

        # Trong self-cooldown → urge giảm, mô phỏng "vừa nói xong nghỉ chút"
        self_cd = self._self_cooldown_remaining(now)
        if self_cd > 0:
            self.urge = max(0.0, self.urge - self.cfg.decay_after_speak)
            return

        # Base rise theo silence — linear đơn giản MVP (spec cho phép rise_curve
        # tuỳ chỉnh sau; hiện tại: rise_base * silence, cap ở rise_max_per_tick)
        silence = now - self.last_external_activity_ts
        base_rise = min(self.cfg.rise_max_per_tick, self.cfg.rise_base * silence)

        # Mood modifier
        mult = 1.0
        mult += (current_mood.bon_chon / 10.0) * self.cfg.bon_chon_weight
        mult -= (current_mood.buon / 10.0) * self.cfg.buon_dampen
        mult -= (current_mood.nguong / 10.0) * self.cfg.nguong_dampen
        mult = max(0.2, mult)

        # Nag penalty: nói ambient liên tiếp không ai phản hồi → giảm dần
        nag = max(0.4, 1.0 - 0.15 * self.consecutive_ignored)

        noise = self._rng.gauss(0.0, self.cfg.urge_noise_std)

        self.urge = _clamp(self.urge + base_rise * mult * nag + noise, 0.0, 100.0)

    def should_speak_now(self) -> bool:
        """Probabilistic — dưới floor: False; trên floor: p sigmoid-like tăng dần."""
        if self.urge < self.cfg.urge_floor:
            return False
        raw_p = (self.urge - self.cfg.urge_floor) / self.cfg.prob_scale
        p = min(self.cfg.prob_max, max(0.0, raw_p))
        fired = self._rng.random() < p
        if fired:
            self._speak_decisions += 1
        return fired

    def on_self_spoke(self) -> None:
        """Sau khi turn ambient hoàn tất (spec 3.a hook)."""
        self.last_self_speak_ts = self._clock()
        self.consecutive_ignored += 1
        self.urge = 0.0

    def on_external_activity(self) -> None:
        """Chat/operator lên tiếng — reset silence timer + nag counter."""
        self.last_external_activity_ts = self._clock()
        self.consecutive_ignored = 0

    def _self_cooldown_remaining(self, now: float) -> float:
        elapsed = now - self.last_self_speak_ts
        return max(0.0, self.cfg.self_cooldown_seconds - elapsed)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "urge": round(self.urge, 2),
            "silence_seconds": round(now - self.last_external_activity_ts, 1),
            "self_cooldown_remaining": round(self._self_cooldown_remaining(now), 1),
            "consecutive_ignored": self.consecutive_ignored,
            "ticks": self._ticks,
            "speak_decisions": self._speak_decisions,
        }


# ─────────────────────── CategorySelector ───────────────────────


class CategorySelector:
    """Chọn category theo weighted random, loại category vừa dùng + đang cooldown.

    Trả None khi tất cả đang cooldown / không candidate — orchestrator xử lý
    (VD fallback share_thought hoặc skip lượt).
    """

    def __init__(
        self,
        cfg: AutonomyConfig,
        clock=None,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock or time.time
        self._rng = rng or random.Random()
        self._recent: deque[str] = deque(maxlen=max(1, cfg.no_repeat_window))
        self._last_used_ts: dict[str, float] = {}
        self._log = get_logger("autonomy.selector")

    def select(self, mood: MoodState) -> str | None:
        """Trả tên category hoặc None nếu không candidate. Không mutate state
        (mark used bằng `mark_used()` sau khi orchestrator confirm dùng)."""
        candidates: list[tuple[str, float]] = []
        for name, c in self.cfg.categories.items():
            if name in self._recent:
                continue
            if self._on_cooldown(name, c.cooldown_seconds):
                continue
            w = c.weight
            for dim, mult in c.mood_boost.items():
                dim_val = float(getattr(mood, dim, 0))
                w *= 1.0 + (dim_val / 10.0) * (mult - 1.0)
            if w > 0:
                candidates.append((name, w))

        if not candidates:
            return None

        return _weighted_choice(candidates, self._rng)

    def mark_used(self, category: str) -> None:
        """Orchestrator gọi sau khi confirm dùng category (VD material có đủ, generate xong)."""
        self._recent.append(category)
        self._last_used_ts[category] = self._clock()

    def recent(self) -> list[str]:
        return list(self._recent)

    def _on_cooldown(self, name: str, cooldown_s: int) -> bool:
        ts = self._last_used_ts.get(name)
        if ts is None:
            return False
        return (self._clock() - ts) < cooldown_s

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "recent": list(self._recent),
            "cooldowns": {
                name: max(0.0, c.cooldown_seconds - (now - self._last_used_ts.get(name, 0)))
                for name, c in self.cfg.categories.items()
                if name in self._last_used_ts
            },
        }


# ─────────────────────── helpers ───────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _weighted_choice(items: list[tuple[str, float]], rng: random.Random) -> str:
    total = sum(w for _, w in items)
    if total <= 0:
        return items[0][0]
    r = rng.uniform(0, total)
    upto = 0.0
    for name, w in items:
        upto += w
        if upto >= r:
            return name
    return items[-1][0]  # numerical safety
