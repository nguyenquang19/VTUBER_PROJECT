import json, socket, os
from shared.utils.logger import get_logger

_log = get_logger("node5-client")
NODE5_HOST = os.getenv("NODE5_HOST", "127.0.0.1")
NODE5_PORT = int(os.getenv("NODE5_PORT", "8805"))

# Client gọi Node5. get_profile có TIMEOUT CỨNG 300ms - lỗi/chậm -> None, không chèn gì.
# Node5 chết KHÔNG được làm sập Node2.
class Node5Client:
    def __init__(self, host=NODE5_HOST, port=NODE5_PORT):
        self._host, self._port = host, port

    def _call(self, req: dict, timeout: float):
        try:
            with socket.create_connection((self._host, self._port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                line = s.makefile().readline()
                return json.loads(line) if line.strip() else None
        except Exception as e:
            _log.info(f"node5 call fail ({req.get('op')}): {e}")
            return None

    # Lấy hồ sơ người quen - 300ms CỨNG, lỗi -> None (không chèn)
    def get_profile(self, user_id: str):
        return self._call({"op": "get_profile", "user_id": user_id}, timeout=0.3)

    # Ghi tóm tắt cuối buổi - job nền, không giới hạn cứng (chạy nền)
    def write_summary(self, user_id, display_name, session_id, summary):
        return self._call({"op": "write_summary", "user_id": user_id,
                           "display_name": display_name, "session_id": session_id,
                           "summary": summary}, timeout=10.0)
                           