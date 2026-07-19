import threading

# Nhận StopSignal -> đánh dấu turn_id bị hủy. Node3 KHÔNG cần biết lý do (Node2 quyết).
class CancelRegistry:
    def __init__(self): self._cancelled = set(); self._lock = threading.Lock()
    def cancel(self, turn_id: str):
        with self._lock: self._cancelled.add(turn_id)
    def is_cancelled(self, turn_id: str) -> bool:
        with self._lock: return turn_id in self._cancelled