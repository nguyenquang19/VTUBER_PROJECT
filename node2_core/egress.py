import json, socket, threading
from shared.contracts.payloads import SentenceOut, StopSignal
from shared.utils.timing_recorder import stamp
from .config import NODE3_HOST, NODE3_PORT, NODE3_STOP_PORT

# 2 kênh sang Node3: (1) luồng câu, (2) stop signal tức thời.
class Node3Egress:
    def __init__(self):
        self._sent_conn = None; self._stop_conn = None
        self._lock = threading.Lock()
    def _conn(self, port):
        return socket.create_connection((NODE3_HOST, port), timeout=2)
    def send_sentence(self, s: SentenceOut, trace=None):
        if trace is not None: stamp(trace, "t7_handoff_node3")
        line = (json.dumps(s.to_dict()) + "\n").encode()
        with self._lock:
            if self._sent_conn is None: self._sent_conn = self._conn(NODE3_PORT)
            self._sent_conn.sendall(line)
    def send_stop(self, turn_id: str):
        # Kênh riêng, gửi NGAY, không đi qua hàng đợi câu.
        line = (json.dumps(StopSignal(turn_id).to_dict()) + "\n").encode()
        c = self._conn(NODE3_STOP_PORT)
        try: c.sendall(line)
        finally: c.close()