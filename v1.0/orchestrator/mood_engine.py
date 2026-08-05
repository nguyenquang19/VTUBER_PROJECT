"""MoodEngine — target-based spring-damper 2 kênh (Phase 7.5.A, spec Mục 5).

Tầng 3 của Emotion Simulation. Nhận `mood_target` (Tầng 2 appraisal, tin cao)
và `llm_mood_hint` (Tầng 4 self-report, tin thấp) → cập nhật position mượt qua
spring-damper. Position là mood hiển thị/animation dùng.

Fix theo spec v2:
- (Lỗi 1) impulse = target mà chiều bị kéo tới, KHÔNG phải velocity
- (Lỗi 2) đơn vị = điểm 0-10 (khớp MoodState)
- (Lỗi 3) target_decay chạy theo elapsed từ lần set gần nhất — KHÔNG idle_factor
- (Lỗi 7) đa sự kiện = saturation: max + 0.5×(n−1), cap 10

Sync API (không async — tick chạy trong loop, apply_* chỉ thao tác dict).
"""
from __future__ import annotations

import math
import time
from typing import Any

from interfaces.animation import MoodState
from orchestrator.logger import get_logger

DIMENSIONS: tuple[str, ...] = ("vui", "buon", "buc", "bon_chon", "nguong")


class MoodEngineError(Exception):
    pass


class MoodEngine:
    def __init__(
        self,
        tick_hz: int = 10,
        stiffness: float = 0.30,
        damping: float = 0.75,
        target_decay_rate: float = 0.15,
        llm_hint_weight: float = 0.20,
        saturation_bonus: float = 0.5,
        baseline: dict[str, float] | None = None,
        clock=None,  # inject để test time-dependent behavior
    ) -> None:
        self.tick_hz = tick_hz
        self.stiffness = float(stiffness)
        self.damping = float(damping)
        self.target_decay_rate = float(target_decay_rate)
        self.llm_hint_weight = float(llm_hint_weight)
        self.saturation_bonus = float(saturation_bonus)
        self.baseline: dict[str, float] = dict(
            baseline or {d: 5.0 for d in DIMENSIONS}
        )
        for d in DIMENSIONS:
            if d not in self.baseline:
                self.baseline[d] = 5.0

        self._clock = clock or time.monotonic
        now = self._clock()
        self.pos: dict[str, float] = dict(self.baseline)
        self.vel: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
        self.target: dict[str, float] = dict(self.baseline)
        self.last_set_ts: dict[str, float] = {d: now for d in DIMENSIONS}

        self._log = get_logger("mood_engine")

        self._appraisal_applies = 0
        self._llm_applies = 0
        self._ticks = 0

    @classmethod
    def from_loader(cls, loader, clock=None) -> "MoodEngine":
        get = lambda k, d=None: loader.get("mood_engine", f"mood_engine.{k}", d)  # noqa: E731
        return cls(
            tick_hz=int(get("tick_hz", 10)),
            stiffness=float(get("stiffness", 0.30)),
            damping=float(get("damping", 0.75)),
            target_decay_rate=float(get("target_decay_rate", 0.15)),
            llm_hint_weight=float(get("llm_hint_weight", 0.20)),
            saturation_bonus=float(get("saturation_bonus", 0.5)),
            baseline=get("baseline", None),
            clock=clock,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "mood_appraisal_applies": self._appraisal_applies,
            "mood_llm_applies": self._llm_applies,
            "mood_ticks": self._ticks,
        }

    # ---------- Kênh A: appraisal ----------

    def apply_appraisal(self, mood_targets: dict[str, float]) -> None:
        """SET target (đã áp modifier + saturation TRƯỚC khi gọi).

        Không tự saturation ở đây — caller (EmotionOrchestrator 7.5.C) đã gộp
        đa sự kiện trong cùng tick trước khi gọi.
        """
        now = self._clock()
        for dim, tgt in mood_targets.items():
            if dim not in self.baseline:
                continue  # bỏ qua dimension không hợp lệ
            self.target[dim] = _clamp(float(tgt), 0.0, 10.0)
            self.last_set_ts[dim] = now
        self._appraisal_applies += 1

    def saturate(self, targets_per_dim: dict[str, list[float]]) -> dict[str, float]:
        """Gộp đa sự kiện trong 1 tick theo Mục 5.4:
          - 1 sự kiện → dùng thẳng
          - Nhiều sự kiện cùng chiều → max + saturation_bonus×(n-1), cap 10
        Trả dict {dim: final_target} ready để apply_appraisal.
        """
        out: dict[str, float] = {}
        for dim, tgts in targets_per_dim.items():
            if not tgts:
                continue
            if len(tgts) == 1:
                out[dim] = _clamp(float(tgts[0]), 0.0, 10.0)
                continue
            top = max(float(t) for t in tgts)
            bonus = self.saturation_bonus * (len(tgts) - 1)
            out[dim] = _clamp(top + bonus, 0.0, 10.0)
        return out

    # ---------- Kênh B: LLM hint ----------

    def apply_llm_hint(self, llm_mood: MoodState) -> None:
        """Nudge target nhẹ về phía LLM tự report (weight thấp, tin thấp)."""
        for dim in DIMENSIONS:
            suggested = float(getattr(llm_mood, dim))
            self.target[dim] = _clamp(
                self.target[dim] + self.llm_hint_weight * (suggested - self.target[dim]),
                0.0, 10.0,
            )
        self._llm_applies += 1

    # ---------- Tick (spring + decay + damping) ----------

    def tick(self, dt: float | None = None) -> MoodState:
        """1 tick physics: target decay → spring force → damping → integrate pos.

        `dt`: seconds since last tick. Nếu None → dùng 1/tick_hz.
        Trả MoodState (int 0-10) — round từ position float.
        """
        if dt is None:
            dt = 1.0 / self.tick_hz
        if dt <= 0:
            raise MoodEngineError(f"dt phải > 0, got {dt}")

        now = self._clock()
        for dim in DIMENSIONS:
            # 1. Target decay về baseline theo elapsed từ lần SET target gần nhất
            elapsed = now - self.last_set_ts[dim]
            decay = min(1.0, self.target_decay_rate * elapsed)
            self.target[dim] += decay * (self.baseline[dim] - self.target[dim])

            # 2. Spring pull + damping
            spring = self.stiffness * (self.target[dim] - self.pos[dim])
            damp = -self.damping * self.vel[dim]
            self.vel[dim] += (spring + damp) * dt

            # 3. NaN/inf guard (defensive — không nên xảy ra với config over-damped)
            if not math.isfinite(self.vel[dim]):
                self.vel[dim] = 0.0
            new_pos = self.pos[dim] + self.vel[dim] * dt
            if not math.isfinite(new_pos):
                new_pos = self.baseline[dim]
            self.pos[dim] = _clamp(new_pos, 0.0, 10.0)
        self._ticks += 1
        return self._to_mood_state()

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Trạng thái đầy đủ cho dashboard/debug."""
        return {
            "pos": dict(self.pos),
            "vel": dict(self.vel),
            "target": dict(self.target),
        }

    def _to_mood_state(self) -> MoodState:
        # Round float pos → int 0-10 (khớp interface Phase 0.B)
        rounded = {d: int(round(_clamp(self.pos[d], 0.0, 10.0))) for d in DIMENSIONS}
        return MoodState(**rounded)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
