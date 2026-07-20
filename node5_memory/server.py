import json, socketserver, os
from .service import Node5Service
PORT = int(os.getenv("NODE5_PORT", "8805"))

def serve(svc=None):
    svc = svc or Node5Service()
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8").strip()
                if not line: continue
                req = json.loads(line)
                op = req.get("op")
                if op == "get_profile":
                    res = svc.get_profile(req["user_id"])
                elif op == "write_summary":
                    res = svc.write_summary(req["user_id"], req["display_name"],
                                            req["session_id"], req["summary"])
                else:
                    res = {"error": "unknown op"}
                self.wfile.write((json.dumps(res, ensure_ascii=False)+"\n").encode("utf-8"))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), H) as s:
        print(f"node5 memory on {PORT}"); s.serve_forever()

if __name__ == "__main__": serve()