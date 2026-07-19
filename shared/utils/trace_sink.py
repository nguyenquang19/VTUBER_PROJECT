import json, socket, os, threading, queue
from shared.contracts.timing import TimingTrace

# Bắn partial timing về collector. KHÔNG chặn đường chính: đẩy vào queue nội bộ,
# 1 luồng nền gửi thật; queue đầy -> bỏ (giống nguyên tắc Node4 GĐ6, mục 16).
_HOST = os.getenv("TIMING_COLLECTOR_HOST", "127.0.0.1")
_PORT = int(os.getenv("TIMING_COLLECTOR_PORT", "8810"))
_ENABLED = os.getenv("TIMING_COLLECTOR", "1") == "1"

class _TraceSink:
    def __init__(self, maxlen=1000):
        self._q = queue.Queue(maxsize=maxlen)
        if _ENABLED:
            threading.Thread(target=self._pump, daemon=True).start()
    def emit(self, node: str, trace: TimingTrace):
        if not _ENABLED: return
        try: self._q.put_nowait((node, trace.to_dict()))
        except queue.Full: pass                      # bỏ, không chặn turn
    def _pump(self):
        while True:
            node, td = self._q.get()
            try:
                line = (json.dumps({"node": node, "timing": td}) + "\n").encode("utf-8")
                with socket.create_connection((_HOST, _PORT), timeout=0.3) as s:
                    s.sendall(line)
            except Exception: pass                    # collector chết -> kệ, không ảnh hưởng chính

_sink = _TraceSink()
def emit_partial(node: str, trace: TimingTrace): _sink.emit(node, trace)