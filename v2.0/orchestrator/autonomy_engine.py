"""AutonomyEngine v2: Urge + CategorySelector + AutonomyEngine composer (Aut.A + C).

Spec: docs/AUTONOMY_ENGINE_REDESIGN.md — thay hard `silence > 60s` bằng urge
accumulator probabilistic + category selector có mood coupling + no-repeat.

AutonomyEngine (composer) compose 5 phần: Urge + Selector + MaterialProvider
+ OpenerTracker + DedupBuffer. Caller (Aut.D wire) tick loop + gọi maybe_generate.

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
from services.autonomy.dedup import DedupBuffer
from services.autonomy.material_provider import MaterialProvider, RuntimeContext
from services.autonomy.opener_tracker import OpenerTracker
from services.autonomy.prompt_builder import render_prompt


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
        """Chat/operator lên tiếng — reset silence timer + nag counter + URGE.

        FIX (2026-08): trước đây KHÔNG reset urge → urge tích dồn qua cuộc trò
        chuyện, Mai tự nói ĐÈ ngay sau khi user vừa nhắn (thay vì đáp), làm loạn
        thứ tự + mất mạch. Có người đang chat = không cần lấp im lặng → urge về 0,
        chỉ tích lại khi im lặng mới xuất hiện."""
        self.last_external_activity_ts = self._clock()
        self.consecutive_ignored = 0
        self.urge = 0.0

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


# ─────────────────────── AutonomyEngine (composer, Aut.C) ───────────────────────


@dataclass
class AmbientDecision:
    """Kết quả maybe_generate — data đủ để caller gọi LLM sinh câu ambient."""
    category: str
    prompt_text: str           # instruction đã slot-filled, inject vào messages[user]
    mood_snapshot: MoodState   # mood lúc quyết định (dùng cho drift + log)
    material: dict             # nguyên liệu đã dùng (dùng cho log/debug)


class AutonomyEngine:
    """Compose 5 phần: Urge + Selector + MaterialProvider + OpenerTracker + DedupBuffer.

    Caller (Aut.D wire — cli.py / stream_runtime.py) chạy tick loop:
      while running:
          await sleep(cfg.tick_seconds)
          engine.tick(current_mood)
          decision = engine.maybe_generate(current_mood, ctx)
          if decision:
              text = await run_llm(decision.prompt_text)
              if engine.check_dedup(text):
                  text = await run_llm(decision.prompt_text)  # regen 1 lần
              # phát TTS + engine.on_self_spoke(text)
    """

    def __init__(
        self,
        cfg: AutonomyConfig,
        material_provider: MaterialProvider,
        opener_tracker: OpenerTracker | None = None,
        dedup_buffer: DedupBuffer | None = None,
        clock=None,
        rng: random.Random | None = None,
        mood_style: Any = None,   # MoodStyleTable | None — self-talk cũng đúng giọng
    ) -> None:
        self.cfg = cfg
        self._rng = rng or random.Random()
        self._clock = clock or time.time
        self._mood_style = mood_style

        self.urge = UrgeAccumulator(cfg.urge, clock=self._clock, rng=self._rng)
        self.selector = CategorySelector(cfg, clock=self._clock, rng=self._rng)
        self.material = material_provider
        self.opener = opener_tracker or OpenerTracker()
        self.dedup = dedup_buffer or DedupBuffer()

        self._log = get_logger("autonomy.engine")
        self._generated_total = 0
        self._skipped_no_material = 0
        self._dedup_hits = 0

    @classmethod
    def from_loader(
        cls,
        loader,
        material_provider: MaterialProvider | None = None,
        rng: random.Random | None = None,
    ) -> "AutonomyEngine":
        cfg = AutonomyConfig.from_loader(loader)
        mp = material_provider or MaterialProvider.from_loader(loader, rng=rng)
        # opener/dedup config từ autonomy_content_pool.yaml
        opener_win = int(loader.get("autonomy_content_pool", "opener_tracker.window", 5))
        opener_words = int(loader.get(
            "autonomy_content_pool", "opener_tracker.words_per_opener", 3,
        ))
        dedup_win = int(loader.get("autonomy_content_pool", "dedup.window", 5))
        dedup_thr = float(loader.get(
            "autonomy_content_pool", "dedup.token_overlap_threshold", 0.6,
        ))
        # T5: mood_style để self-talk cũng đổi giọng theo mood (None-safe)
        try:
            from services.emotion.mood_style import MoodStyleTable
            mood_style = MoodStyleTable.from_loader(loader)
        except Exception:
            mood_style = None
        return cls(
            cfg=cfg,
            material_provider=mp,
            opener_tracker=OpenerTracker(window=opener_win, words_per_opener=opener_words),
            dedup_buffer=DedupBuffer(window=dedup_win, threshold=dedup_thr),
            rng=rng,
            mood_style=mood_style,
        )

    # ---------- lifecycle hooks (caller gọi) ----------

    def tick(self, mood: MoodState) -> None:
        self.urge.tick(mood)

    def on_external_activity(self) -> None:
        """Chat/operator lên tiếng → reset silence + nag."""
        self.urge.on_external_activity()

    def on_self_spoke(self, text: str) -> None:
        """Sau khi turn ambient hoàn tất — record opener + dedup + reset urge."""
        self.urge.on_self_spoke()
        self.opener.record(text)
        self.dedup.record(text)

    # ---------- decision ----------

    def maybe_generate(
        self, mood: MoodState, ctx: RuntimeContext,
    ) -> AmbientDecision | None:
        """Quyết định có sinh ambient turn không. Return None nếu:
          - urge chưa đủ (probabilistic)
          - Tất cả category đang cooldown / trong no_repeat window
          - Tất cả category còn candidate đều thiếu material
        """
        if not self.urge.should_speak_now():
            return None
        return self._pick_and_render(mood, ctx)

    def force_generate(
        self, mood: MoodState, ctx: RuntimeContext,
    ) -> AmbientDecision | None:
        """C0.4: Director đã QUYẾT self_talk (dead-air/cold) → sinh bỏ qua gate urge.
        Vẫn None nếu không cat nào có material (không bịa từ số 0)."""
        return self._pick_and_render(mood, ctx)

    def force_generate_for(
        self, category: str, mood: MoodState, ctx: RuntimeContext,
    ) -> AmbientDecision | None:
        """Render one Director-selected grounded category without random fallback."""
        config = self.cfg.categories.get(category)
        if config is None:
            return None
        material = self.material.get(category, ctx)
        if material is None:
            self._skipped_no_material += 1
            return None
        self.selector.mark_used(category)
        prompt_text = render_prompt(
            category=category,
            material=material,
            mood=mood,
            forbidden_openers=self.opener.forbidden_list(),
            prompt_hint=config.prompt_hint,
            mood_style=self._mood_style,
        )
        self._generated_total += 1
        return AmbientDecision(category, prompt_text, mood, material)

    def _pick_and_render(
        self, mood: MoodState, ctx: RuntimeContext,
    ) -> AmbientDecision | None:
        chosen_cat: str | None = None
        chosen_material: dict | None = None
        # Loop tối đa 2×len(categories) để tìm cat có material (weighted random
        # có thể lặp — không guarantee mỗi lần khác)
        max_tries = max(4, 2 * len(self.cfg.categories))
        tried: set[str] = set()
        for _ in range(max_tries):
            cat = self.selector.select(mood)
            if cat is None:
                break
            if cat in tried:
                continue
            tried.add(cat)
            mat = self.material.get(cat, ctx)
            if mat is None:
                self._skipped_no_material += 1
                continue
            chosen_cat = cat
            chosen_material = mat
            break

        if chosen_cat is None or chosen_material is None:
            return None

        # Mark used TRƯỚC khi return (composer đã quyết dùng)
        self.selector.mark_used(chosen_cat)

        prompt_text = render_prompt(
            category=chosen_cat,
            material=chosen_material,
            mood=mood,
            forbidden_openers=self.opener.forbidden_list(),
            prompt_hint=self.cfg.categories[chosen_cat].prompt_hint,
            mood_style=self._mood_style,
        )
        self._generated_total += 1
        return AmbientDecision(
            category=chosen_cat,
            prompt_text=prompt_text,
            mood_snapshot=mood,
            material=chosen_material,
        )

    def check_dedup(self, text: str) -> bool:
        """True nếu text quá giống câu tự nói gần đây — composer nên regen 1 lần."""
        hit = self.dedup.check(text)
        if hit:
            self._dedup_hits += 1
        return hit

    def get_metrics(self) -> dict[str, Any]:
        return {
            "autonomy_generated_total": self._generated_total,
            "autonomy_skipped_no_material": self._skipped_no_material,
            "autonomy_dedup_hits": self._dedup_hits,
            **self.urge.snapshot(),
            **{f"selector_{k}": v for k, v in self.selector.snapshot().items()},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "urge": self.urge.snapshot(),
            "selector": self.selector.snapshot(),
            "opener_recent": self.opener.recent(),
            "dedup_recent_count": len(self.dedup.recent()),
            "generated_total": self._generated_total,
        }
