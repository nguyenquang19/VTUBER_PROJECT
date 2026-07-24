import itertools, threading, time
from .config import (
    CHAT_TTL_SEC, AUTO_TTL_SEC,
    SCORE_CRISIS, SCORE_CHAT_BASE, SCORE_AUTO_BASE,
    SCORE_MENTION_MAI_BONUS, SCORE_TIER1_WEIGHT,
    SCORE_CHAT_WAIT_PER_SEC, SCORE_AUTO_WAIT_PENALTY_PER_SEC,
)
from shared.contracts.payloads import Priority
from shared.utils.text import mentions_mai


def compute_score(priority: Priority, rec, now: float, enqueued_at: float) -> float:
    """Điểm ưu tiên động. CRISIS luôn cố định, cao nhất, không gì vượt qua.
    Con số khởi điểm — chỉnh sau khi chạy thật (nguyên tắc 4)."""
    if priority == Priority.CRISIS:
        return SCORE_CRISIS

    score = SCORE_CHAT_BASE if priority == Priority.CHAT else SCORE_AUTO_BASE

    content = getattr(rec, "content", "") or ""
    if mentions_mai(content):
        score += SCORE_MENTION_MAI_BONUS

    score += (getattr(rec, "tier1_score", 0.0) or 0.0) * SCORE_TIER1_WEIGHT

    wait = max(0.0, now - enqueued_at)
    if priority == Priority.CHAT:
        score += wait * SCORE_CHAT_WAIT_PER_SEC       # chờ lâu -> cộng dần
    else:
        score -= wait * SCORE_AUTO_WAIT_PENALTY_PER_SEC  # AUTO chờ lâu -> trừ dần

    return score


def _ttl_for(priority: Priority) -> float | None:
    if priority == Priority.CHAT:
        return CHAT_TTL_SEC
    if priority == Priority.AUTO:
        return AUTO_TTL_SEC
    return None   # CRISIS không hết hạn


# Hàng đợi ưu tiên theo điểm số động (Phần 5). CRISIS > CHAT > AUTO nhưng
# trong cùng loại, điểm phụ thuộc thời gian chờ + tín hiệu nội dung.
# Quy mô nhỏ (vài chục item) -> quét toàn bộ danh sách lúc get(), đơn giản
# hơn heap tự cân bằng lại theo thời gian.
class PriorityInbox:
    def __init__(self):
        self._items = []          # list[(priority, counter, record, enqueued_at)]
        self._c = itertools.count()
        self._lock = threading.Lock()
        self._crisis_evt = threading.Event()

    def put(self, priority: Priority, record):
        with self._lock:
            self._items.append((priority, next(self._c), record, time.time()))
            if priority == Priority.CRISIS:
                self._crisis_evt.set()

    def get(self):
        with self._lock:
            now = time.time()
            self._drop_expired(now)
            if not self._items:
                return None
            best_i = 0
            best_key = None
            for i, (pri, cnt, rec, enq) in enumerate(self._items):
                key = (compute_score(pri, rec, now, enq), -cnt)
                if best_key is None or key > best_key:
                    best_key = key
                    best_i = i
            pri, _, rec, _ = self._items.pop(best_i)
            return pri, rec

    def _drop_expired(self, now: float):
        kept = []
        for item in self._items:
            pri, _, _, enq = item
            ttl = _ttl_for(pri)
            if ttl is not None and (now - enq) > ttl:
                continue
            kept.append(item)
        self._items = kept

    def has_pending_crisis(self) -> bool:
        with self._lock:
            return any(pri == Priority.CRISIS for pri, _, _, _ in self._items)

    def crisis_event(self) -> threading.Event:
        return self._crisis_evt

    def get_size(self) -> int:
        with self._lock:
            return len(self._items)
