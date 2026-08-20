"""Round-robin seed-content pool with a bounded no-repeat window.

MaterialProvider dùng để cấp seed (chủ đề/câu hỏi) cho LLM thay vì để LLM tự bịa.
Đảm bảo đa dạng chủ đề bằng cách xoay vòng + không dùng lại N seed gần nhất.
"""
from __future__ import annotations

import random
from collections import deque
from typing import Iterable


class RoundRobinPool:
    def __init__(
        self,
        items: Iterable[str],
        no_repeat_last_n: int = 8,
        reshuffle_when_exhausted: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._items: list[str] = [s for s in items if s]
        if not self._items:
            raise ValueError("pool rỗng")
        self._no_repeat = max(0, no_repeat_last_n)
        self._reshuffle = reshuffle_when_exhausted
        self._rng = rng or random.Random()
        self._recent: deque[str] = deque(maxlen=self._no_repeat)

    def next(self) -> str | None:
        """Lấy 1 seed chưa dùng trong window. None nếu không còn candidate
        (chỉ xảy ra khi reshuffle=False và toàn bộ pool trong window)."""
        candidates = [x for x in self._items if x not in self._recent]
        if not candidates:
            if not self._reshuffle:
                return None
            # Reshuffle: clear recent để pool available lại
            self._recent.clear()
            candidates = list(self._items)
        pick = self._rng.choice(candidates)
        self._recent.append(pick)
        return pick

    def snapshot(self) -> dict:
        return {
            "size": len(self._items),
            "recent": list(self._recent),
            "available": len(self._items) - len(self._recent),
        }
