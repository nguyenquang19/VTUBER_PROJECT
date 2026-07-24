import time
from collections import deque
from shared.utils.clock import mono_ms
from shared.utils.text import mentions_mai
from .config import RATE_WINDOW_S, ROOM_WINDOW_S, ROOM_MSG_LIMIT, ROOM_HIGH_SCORE

class RateLimiter:
    def __init__(self, window_s: float = RATE_WINDOW_S,
                 room_window_s: float = ROOM_WINDOW_S,
                 room_limit: int = ROOM_MSG_LIMIT):
        self._w = window_s * 1000; self._last: dict[str, float] = {}
        self._room_w = room_window_s
        self._room_limit = room_limit
        self._room_events: deque = deque()   # timestamp (s) mỗi tin ĐÃ QUA lọc nhiễu

    def allow(self, user_id: str) -> bool:
        t = mono_ms(); prev = self._last.get(user_id)
        if prev is not None and t - prev < self._w: return False
        self._last[user_id] = t; return True

    def allow_room(self, content: str, score: float) -> bool:
        """Giới hạn toàn phòng. Gọi SAU khi đã qua lọc nhiễu (2.3) -> chỉ đếm
        tin có nội dung thật. Phòng đông mà chưa vượt ngưỡng -> luôn nhận.
        Vượt ngưỡng -> chỉ nhận tin nhắc tên Mai hoặc tier1_score cao."""
        now = time.time()
        self._room_events.append(now)
        cutoff = now - self._room_w
        while self._room_events and self._room_events[0] < cutoff:
            self._room_events.popleft()

        if len(self._room_events) <= self._room_limit:
            return True

        return mentions_mai(content) or score >= ROOM_HIGH_SCORE
