import threading, time, requests
from shared.utils.logger import get_logger
from ..config import LLM_HOST

_log = get_logger("health")

# Ping /health định kỳ. Giữ trạng thái up/down để orchestrator hỏi nhanh,
# KHÔNG phải đợi timeout request thật mới biết LLM chết.
class LLMHealthMonitor:
    def __init__(self, host=LLM_HOST, interval=5.0, timeout=2.0):
        self._host, self._interval, self._timeout = host, interval, timeout
        self._healthy = True
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            ok = self._probe()
            with self._lock:
                if ok != self._healthy:
                    _log.info(f"LLM health -> {'UP' if ok else 'DOWN'}")
                self._healthy = ok
            time.sleep(self._interval)

    def _probe(self) -> bool:
        try:
            # llama.cpp có /health; fallback /v1/models nếu build khác
            r = requests.get(f"{self._host}/health", timeout=self._timeout)
            return r.status_code == 200
        except Exception:
            try:
                r = requests.get(f"{self._host}/v1/models", timeout=self._timeout)
                return r.status_code == 200
            except Exception:
                return False

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def stop(self):
        self._stop.set()