import json, socketserver
from shared.contracts.payloads import IngestionRecord, EventType
from shared.contracts.timing import TimingTrace
from .config import INGRESS_PORT

def _to_record(d: dict) -> IngestionRecord:
    t = TimingTrace(**d["timing"])
    return IngestionRecord(EventType(d["event_type"]), d["user_id"],
                           d["display_name"], d["content"], d["sent_at"],
                           d["tier1_score"], t, d.get("facts"))

def serve(orch, port=INGRESS_PORT):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8")      # ÉP UTF-8, không để locale quyết
                if not line.strip(): continue
                orch.submit(_to_record(json.loads(line)))
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), H) as s:
        print(f"node2 ingress on {port}"); s.serve_forever()