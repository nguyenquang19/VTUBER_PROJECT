import json, socketserver, threading
from .config import RECV_PORT

def serve_receiver(dash_state):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8").strip()
                if not line: continue
                try: dash_state.apply(json.loads(line))
                except Exception: pass
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("0.0.0.0", RECV_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv