import re
import threading
import time
from collections import deque

_SHORT_REACTIONS = {
    "ừ", "uh", "ok", "oke", "okay", "haha", "hihi", "hehe", "kkk",
    "vâng", "dạ", "=))", "=)))",
}

_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
_REPEATED_CHAR = re.compile(r"^(.)\1{2,}$", re.UNICODE)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def is_noise(content: str) -> bool:
    """True nếu tin không đáng tạo việc cho Mai."""
    text = (content or "").strip()

    if len(text) < 3:
        return True

    if not _HAS_LETTER.search(text):
        return True   # chỉ gồm emoji/ký hiệu/số, không có chữ cái

    low = text.lower()
    if low in _SHORT_REACTIONS:
        return True

    if _PUNCT_ONLY.match(text):
        return True   # dấu câu đơn

    if _REPEATED_CHAR.match(low):
        return True   # lặp một ký tự: aaaaa, !!!!!

    return False


class RoomPulse:
    """Đếm nhịp phòng. Thread-safe. Tin nhiễu vẫn được đếm vào đây."""

    def __init__(self, window_sec: float = 30.0, keep_content: int = 10):
        self._window_sec = window_sec
        self._keep_content = keep_content
        self._lock = threading.Lock()
        self._events = deque()          # (ts, user_id, is_noise)
        self._recent_content = deque(maxlen=keep_content)  # "tên: nội dung"

    def record(self, user_id: str, content: str, is_noise: bool) -> None:
        now = time.time()
        with self._lock:
            self._events.append((now, user_id, is_noise))
            self._trim(now)
            if not is_noise and content and content.strip():
                self._recent_content.append(f"{user_id}: {content.strip()}")

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)
            total = len(self._events)
            noisy = sum(1 for _, _, n in self._events if n)
            active_users = {uid for _, uid, _ in self._events}
            return {
                "msgs_30s": total,
                "active_users_30s": len(active_users),
                "noise_ratio": (noisy / total) if total else 0.0,
                "recent_content": list(self._recent_content),
            }