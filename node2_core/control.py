import json, socketserver, threading, os
from shared.utils.logger import get_logger

_log = get_logger("control")
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "8821"))

# Cổng RIÊNG nhận lệnh điều khiển (KHÔNG qua Node4). Nếu cổng này chết,
# pipeline vẫn chạy — chỉ mất điều khiển tay. orchestrator tự xử lý lệnh.
def serve_control(orch, port=CONTROL_PORT):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8").strip()
                if not line: continue
                try:
                    cmd = json.loads(line)
                    if cmd.get("action") == "skip":
                        orch.skip(cmd.get("turn_id", ""))   # dùng lại StopSignal
                        _log.info(f"SKIP {cmd.get('turn_id') or 'current'}")
                except Exception as e:
                    _log.info(f"bad control: {e}")
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), H) as s:
        print(f"control on {port}"); s.serve_forever()