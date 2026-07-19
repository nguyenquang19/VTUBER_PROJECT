from shared.utils.clock import mono_ms
from .config import RATE_WINDOW_S

class RateLimiter:
    def __init__(self, window_s: float = RATE_WINDOW_S):
        self._w = window_s * 1000; self._last: dict[str, float] = {}
    def allow(self, user_id: str) -> bool:
        t = mono_ms(); prev = self._last.get(user_id)
        if prev is not None and t - prev < self._w: return False
        self._last[user_id] = t; return True