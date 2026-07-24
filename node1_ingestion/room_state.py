import json
import os
import threading
import time

# node1 công bố nhịp phòng + voice cho node6 đọc (file JSON). Hai node tách rời
# -> giao tiếp qua file, giống mood.db. Best-effort: lỗi ghi bỏ qua, không chặn
# bot. Ghi nguyên tử (tmp + replace) để node6 không đọc trúng file dở.
class RoomStatePublisher:
    def __init__(self, pulse, voice_members_fn=None,
                 path="data/room_state.json", interval=2.0):
        self._pulse = pulse
        self._voice_members_fn = voice_members_fn   # () -> list[str] | None
        self._path = path
        self._interval = interval
        self._last_chat_ts = 0.0
        self._lock = threading.Lock()

    def note_chat(self):
        """Gọi mỗi tin người thật (kể cả nhiễu) -> chat_silent tính đúng nhịp."""
        with self._lock:
            self._last_chat_ts = time.time()

    def _voice_members(self):
        if not self._voice_members_fn:
            return []
        try:
            return self._voice_members_fn() or []
        except Exception:
            return []

    def snapshot(self) -> dict:
        now = time.time()
        try:
            pulse = self._pulse.snapshot()
        except Exception:
            pulse = {}
        with self._lock:
            last_chat = self._last_chat_ts
        chat_silent = (now - last_chat) if last_chat > 0 else None
        members = self._voice_members()
        return {
            "voice_members": members,
            "voice_count": len(members),
            "chat_silent_sec": chat_silent,
            "room_pulse": pulse,
            # Discord chưa báo ai đang nói bằng giọng -> để False (Mai "bị điếc", §9).
            "someone_speaking": False,
            "ts": now,
        }

    def write_once(self):
        data = self.snapshot()
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self._path)     # nguyên tử

    def start(self):
        def loop():
            while True:
                try:
                    self.write_once()
                except Exception:
                    pass
                time.sleep(self._interval)
        threading.Thread(target=loop, daemon=True).start()
