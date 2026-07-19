import heapq, itertools, threading
from .config import CTX_BUDGET_CHARS  # noqa
from shared.contracts.payloads import Priority

# Priority queue: crisis > chat > auto. Thread-safe, có báo "có crisis mới".
class PriorityInbox:
    def __init__(self):
        self._h = []; self._c = itertools.count()
        self._lock = threading.Lock()
        self._crisis_evt = threading.Event()
    def put(self, priority: Priority, record):
        with self._lock:
            # heapq min-heap -> đảo dấu priority để lớn nhất ra trước
            heapq.heappush(self._h, (-int(priority), next(self._c), record))
            if priority == Priority.CRISIS:
                self._crisis_evt.set()
    def get(self):
        with self._lock:
            if not self._h: return None
            neg, _, rec = heapq.heappop(self._h)
            return Priority(-neg), rec
    def has_pending_crisis(self) -> bool:
        with self._lock:
            return any(-p == int(Priority.CRISIS) for p, _, _ in self._h)
    def crisis_event(self) -> threading.Event:
        return self._crisis_evt
    def get_size(self) -> int:
        with self._lock: return len(self._h)
    def get_size(self) -> int:
        with self._lock:
            return len(self._h)