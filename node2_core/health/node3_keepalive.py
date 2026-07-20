import socket, threading, time
from shared.utils.logger import get_logger
from ..config import NODE3_HOST, NODE3_PORT

_log = get_logger("health")

# Kiểm tra Node3 còn nghe cổng không (2 máy khác nhau -> kết nối có thể rớt).
class Node3Keepalive:
    def __init__(self, host=NODE3_HOST, port=NODE3_PORT, interval=10.0):
        self._host, self._port, self._interval = host, port, interval
        self._reachable = True; self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            ok = self._probe()
            with self._lock:
                if ok != self._reachable:
                    _log.info(f"Node3 -> {'REACHABLE' if ok else 'UNREACHABLE'}")
                self._reachable = ok
            time.sleep(self._interval)

    def _probe(self) -> bool:
        try:
            with socket.create_connection((self._host, self._port), timeout=2):
                return True
        except Exception:
            return False

    def is_reachable(self) -> bool:
        with self._lock: return self._reachable

    def stop(self): self._stop.set()