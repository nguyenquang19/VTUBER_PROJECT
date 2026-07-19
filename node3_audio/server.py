import json, socketserver, threading
from shared.contracts.payloads import SentenceOut, Mood
from shared.contracts.timing import TimingTrace
from .config import SENT_PORT, STOP_PORT
def _sentence(d: dict) -> SentenceOut:
    return SentenceOut(d["turn_id"], d["seq"], d["text"],
                       d["is_last"], Mood(d["mood"]))

def serve(node3):
    class SentH(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8")
                if not line.strip(): continue
                d = json.loads(line)
                trace = TimingTrace(**d["timing"]) if "timing" in d else None
                node3.on_sentence(_sentence(d), trace)
    class StopH(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8")
                if not line.strip(): continue
                node3.stop(json.loads(line)["turn_id"])

    def _run(handler, port, tag):
        with socketserver.ThreadingTCPServer(("0.0.0.0", port), handler) as s:
            print(f"node3 {tag} on {port}"); s.serve_forever()

    threading.Thread(target=_run, args=(StopH, STOP_PORT, "STOP"), daemon=True).start()
    _run(SentH, SENT_PORT, "SENT")

if __name__ == "__main__":
    from .node3 import Node3
    serve(Node3())