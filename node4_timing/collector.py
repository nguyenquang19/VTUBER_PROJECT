import json, socketserver, threading
from shared.contracts.timing import TimingTrace
from shared.utils.logger import get_logger
from .report import format_breakdown

_log = get_logger("timing-collector")

# Merge partial theo turn_id. Mỗi node chỉ điền mốc của mình -> chỉ nhận mốc non-None,
# KHÔNG ghi đè mốc đã có (giữ nguyên tắc "không sửa mốc node khác").
class TimingCollector:
    _FIELDS = ("t0_received","t1_enqueued","t2_llm_start","t3_llm_end",
               "t4_moderation_end","t7_handoff_node3","t8_node3_received","t9_audio_start")
    def __init__(self):
        self._acc: dict[str, dict] = {}; self._lock = threading.Lock()
    def ingest(self, timing: dict):
        tid = timing["turn_id"]
        with self._lock:
            cur = self._acc.setdefault(tid, {"turn_id": tid})
            for f in self._FIELDS:
                v = timing.get(f)
                if v is not None and cur.get(f) is None:   # chỉ điền chỗ trống
                    cur[f] = v
            merged = dict(cur)
        # Đủ 2 đầu t0 & t9 -> chốt & in breakdown.
        if merged.get("t0_received") is not None and merged.get("t9_audio_start") is not None:
            self._finalize(tid, merged)
    def _finalize(self, tid, merged):
        trace = TimingTrace(**{k: merged.get(k) for k in ("turn_id", *self._FIELDS)})
        _log.info(format_breakdown(trace))
        # GHI RA FILE để tổng hợp/phân tích
        import json, os
        os.makedirs("data", exist_ok=True)
        with open("data/timing.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn_id": tid, "deltas": trace.deltas()},
                               ensure_ascii=False) + "\n")
        with self._lock: self._acc.pop(tid, None)

def serve(collector, port=8810):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8")
                if not line.strip(): continue
                collector.ingest(json.loads(line)["timing"])
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), H) as s:
        print(f"timing collector on {port}"); s.serve_forever()

if __name__ == "__main__":
    serve(TimingCollector())