import json, socket, threading, queue, time
from shared.contracts.events import DashboardEvent, EventKind
from .config import NODE3_HOST  # dùng chung host

import os
DASH_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASH_PORT = int(os.getenv("DASHBOARD_PORT", "8820"))
DASH_ENABLED = os.getenv("DASHBOARD", "1") == "1"

# Bắn event về Node4. KHÔNG chặn pipeline: đẩy vào queue có GIỚI HẠN,
# đầy -> BỎ event mới (đúng nguyên tắc mục 16 / GĐ6). Luồng nền gửi thật.
class DashboardEmitter:
    def __init__(self, maxlen=500):
        self._q = queue.Queue(maxsize=maxlen)
        if DASH_ENABLED:
            threading.Thread(target=self._pump, daemon=True).start()
    def emit(self, kind: EventKind, **data):
        if not DASH_ENABLED: return
        ev = DashboardEvent(kind, time.time(), data)
        try: self._q.put_nowait(ev)
        except queue.Full: pass          # BỎ, không chặn nơi phát
    def _pump(self):
        while True:
            ev = self._q.get()
            try:
                line = (json.dumps(ev.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
                with socket.create_connection((DASH_HOST, DASH_PORT), timeout=0.3) as s:
                    s.sendall(line)
            except Exception: pass        # Node4 chết -> kệ, pipeline không ảnh hưởng

_emitter = DashboardEmitter()
def emit_event(kind, **data): _emitter.emit(kind, **data)