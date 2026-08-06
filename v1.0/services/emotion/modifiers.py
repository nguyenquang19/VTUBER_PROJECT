"""ModifierEngine — 3 modifier nhân hệ số target (Phase 7.5.B, spec Mục 4.1).

Modifier KHÔNG sinh target riêng — nhân/cộng lên target của category khác. Áp
TRƯỚC khi đưa vào MoodEngine.apply_appraisal.

- mod_first_time: category X lần đầu → target × 1.2 (bất ngờ hơn)
- mod_repeated_troll: mỗi hit thứ N trong session → +0.5 vào buc (luỹ tiến)
- mod_repeated_shutdown: ≥3 shutdown trong 7 ngày → target × 1.3

Memory query async → wrap trong đây. Nếu memory=None → mọi modifier no-op
(fail-safe — không kỳ vọng ép user phải setup memory chỉ để có mood).
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

from interfaces.memory import MemoryTier
from orchestrator.logger import get_logger

# A4: category tiêu cực (dồn grudge) vs tích cực (reset grudge).
_NEGATIVE_CATS = frozenset({
    "chat_insult_troll", "chat_jailbreak_attempt", "chat_sexual_advance",
    "chat_spam_flood",
})
_POSITIVE_CATS = frozenset({
    "chat_compliment", "donation_small", "donation_large", "subscribe_new",
})


class ModifierEngine:
    def __init__(
        self,
        memory: Any = None,   # MemoryService | None
        repeated_shutdown_window_days: int = 7,
        repeated_shutdown_threshold: int = 3,
        repeated_shutdown_multiplier: float = 1.3,
        repeated_troll_bonus_per_hit: float = 0.5,
        first_time_multiplier: float = 1.2,
        grudge_window_seconds: float = 900.0,
        grudge_bonus_per_level: float = 0.5,
        grudge_max_bonus: float = 1.5,
        grudge_max_level: int = 3,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._memory = memory
        self._shutdown_window = timedelta(days=repeated_shutdown_window_days)
        self._shutdown_threshold = repeated_shutdown_threshold
        self._shutdown_mult = float(repeated_shutdown_multiplier)
        self._troll_bonus = float(repeated_troll_bonus_per_hit)
        self._first_time_mult = float(first_time_multiplier)

        # A4 grudge params (cap + decay chống toxic)
        self._grudge_window = float(grudge_window_seconds)
        self._grudge_bonus = float(grudge_bonus_per_level)
        self._grudge_max_bonus = float(grudge_max_bonus)
        self._grudge_max_level = int(grudge_max_level)
        self._clock = clock or time.time
        # viewer_id → (last_negative_ts, level)
        self._grudge: dict[str, tuple[float, int]] = {}

        # In-memory session state (reset khi service restart)
        self._session_troll_count: int = 0
        self._session_seen_categories: set[str] = set()

        self._log = get_logger("modifier_engine")
        self._applies_first_time = 0
        self._applies_repeated_troll = 0
        self._applies_repeated_shutdown = 0
        self._applies_grudge = 0

    @classmethod
    def from_loader(cls, loader, memory: Any = None) -> "ModifierEngine":
        get = lambda k, d=None: loader.get("emotion_appraisal", f"modifiers.{k}", d)  # noqa: E731
        return cls(
            memory=memory,
            repeated_shutdown_window_days=int(get("repeated_shutdown_window_days", 7)),
            repeated_shutdown_threshold=int(get("repeated_shutdown_threshold", 3)),
            repeated_shutdown_multiplier=float(get("repeated_shutdown_multiplier", 1.3)),
            repeated_troll_bonus_per_hit=float(get("repeated_troll_bonus_per_hit", 0.5)),
            first_time_multiplier=float(get("first_time_multiplier", 1.2)),
            grudge_window_seconds=float(get("grudge_window_seconds", 900.0)),
            grudge_bonus_per_level=float(get("grudge_bonus_per_level", 0.5)),
            grudge_max_bonus=float(get("grudge_max_bonus", 1.5)),
            grudge_max_level=int(get("grudge_max_level", 3)),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "mod_first_time_applies": self._applies_first_time,
            "mod_repeated_troll_applies": self._applies_repeated_troll,
            "mod_repeated_shutdown_applies": self._applies_repeated_shutdown,
            "mod_grudge_applies": self._applies_grudge,
            "mod_grudge_active_viewers": len(self._grudge),
            "mod_session_troll_count": self._session_troll_count,
        }

    def reset_session(self) -> None:
        """Gọi khi stream/session mới bắt đầu (repeated_troll count + grudge reset)."""
        self._session_troll_count = 0
        self._session_seen_categories.clear()
        self._grudge.clear()

    # ---------- A4 grudge ----------

    def _grudge_bonus_for(self, viewer_id: str | None, now: float) -> float:
        """Bonus buc từ grudge của viewer (đã decay). 0 nếu hết hạn/không có."""
        if not viewer_id:
            return 0.0
        entry = self._grudge.get(viewer_id)
        if entry is None:
            return 0.0
        last_ts, level = entry
        if (now - last_ts) > self._grudge_window:   # DECAY: quá window → hết grudge
            del self._grudge[viewer_id]
            return 0.0
        return min(self._grudge_max_bonus, self._grudge_bonus * level)

    def _bump_grudge(self, viewer_id: str | None, now: float) -> None:
        """Người vừa tiêu cực → dồn grudge (cap level, KHÔNG leo thang vô hạn)."""
        if not viewer_id:
            return
        entry = self._grudge.get(viewer_id)
        level = 1 if entry is None or (now - entry[0]) > self._grudge_window else entry[1] + 1
        self._grudge[viewer_id] = (now, min(self._grudge_max_level, level))

    def _reset_grudge(self, viewer_id: str | None) -> None:
        """Tương tác tích cực → xoá grudge (tha)."""
        if viewer_id and viewer_id in self._grudge:
            del self._grudge[viewer_id]

    async def apply(
        self,
        category: str,
        targets: dict[str, float],
        viewer_id: str | None = None,
    ) -> dict[str, float]:
        """Trả target đã nhân/cộng modifier. Empty targets → empty out."""
        # A4 grudge: cập nhật state theo viewer TRƯỚC (kể cả targets rỗng, để
        # tương tác tích cực vẫn reset được grudge). now từ clock inject.
        now = self._clock()
        if category in _POSITIVE_CATS:
            self._reset_grudge(viewer_id)

        if not targets:
            return dict(targets)
        out = dict(targets)

        # 1. mod_repeated_troll: luỹ tiến buc trong session
        if category == "chat_insult_troll":
            self._session_troll_count += 1
            if "buc" in out and self._session_troll_count > 1:
                out["buc"] = min(
                    10.0,
                    out["buc"] + self._troll_bonus * (self._session_troll_count - 1),
                )
                self._applies_repeated_troll += 1

        # 1b. A4 grudge: người vừa tiêu cực trước đó → lượt này với người ĐÓ gắt
        #     hơn (buc). Áp bonus TRƯỚC khi bump (grudge từ lần trước, không tự cộng).
        if "buc" in out:
            gb = self._grudge_bonus_for(viewer_id, now)
            if gb > 0:
                out["buc"] = min(10.0, out["buc"] + gb)
                self._applies_grudge += 1
        if category in _NEGATIVE_CATS:
            self._bump_grudge(viewer_id, now)

        # 2. mod_repeated_shutdown: query memory, nếu ≥threshold trong window → ×mult
        if category == "operator_sudden_shutdown" and self._memory is not None:
            try:
                past = await self._memory.query(
                    "operator_sudden_shutdown", top_k=10,
                    tier=MemoryTier.PERSISTENT,
                )
                cutoff = datetime.now() - self._shutdown_window
                recent = [e for e in past if e.timestamp >= cutoff and
                          "operator_sudden_shutdown" in e.tags]
                if len(recent) >= self._shutdown_threshold:
                    for d in out:
                        out[d] = min(10.0, out[d] * self._shutdown_mult)
                    self._applies_repeated_shutdown += 1
            except Exception as e:
                self._log.warning("mod_shutdown_query_failed", error=str(e))
                # fail-safe: bỏ qua modifier, dùng target gốc

        # 3. mod_first_time: session chưa gặp + memory không có
        is_first = category not in self._session_seen_categories
        self._session_seen_categories.add(category)
        if is_first and category not in ("chat_neutral", "chat_question_normal"):
            has_past = await self._check_memory_seen(category, viewer_id)
            if not has_past:
                for d in out:
                    out[d] = min(10.0, out[d] * self._first_time_mult)
                self._applies_first_time += 1

        return out

    async def _check_memory_seen(self, category: str, viewer_id: str | None) -> bool:
        """Query memory xem category có trong lịch sử chưa. False → first-time."""
        if self._memory is None:
            return False  # không có memory → luôn coi first_time (fail-safe)
        try:
            past = await self._memory.query(
                category, top_k=1, viewer_id=viewer_id,
            )
            return bool(past)
        except Exception as e:
            self._log.warning("mod_first_time_query_failed", error=str(e))
            return True  # lỗi → coi như đã gặp (tránh boost sai)
