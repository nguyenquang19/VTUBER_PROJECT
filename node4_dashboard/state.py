import threading, collections

# Gom trạng thái mới nhất để TUI vẽ. Chỉ giữ gần đây, không lưu vô hạn.
class DashState:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = "?"; self.queue_size = 0
        self.last_reply = ""; self.current_turn = ""
        self.chats = collections.deque(maxlen=8)
        self.crises = collections.deque(maxlen=5)
        self.timings = collections.deque(maxlen=5)
    def apply(self, ev: dict):
        with self._lock:
            k, d = ev["kind"], ev.get("data", {})
            if k == "state": self.state = d.get("state", "?")
            elif k == "queue": self.queue_size = d.get("size", 0)
            elif k == "chat": self.chats.append(f"{d.get('user','?')}: {d.get('content','')}")
            elif k == "reply": self.last_reply = d.get("text", "")
            elif k == "crisis": self.crises.append(d.get("content", ""))
            elif k == "timing": self.timings.append(d.get("deltas", {}))
            elif k == "audio": self.current_turn = d.get("turn_id", "")
    def snapshot(self):
        with self._lock:
            return dict(state=self.state, queue=self.queue_size,
                        reply=self.last_reply, chats=list(self.chats),
                        crises=list(self.crises), timings=list(self.timings))