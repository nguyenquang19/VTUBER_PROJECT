import json, socketserver, os
from shared.utils.logger import get_logger

_log = get_logger("voice")
VOICE_PLAY_PORT = int(os.getenv("VOICE_PLAY_PORT", "8806"))

# Nhận PlayEvent từ Node3 -> đẩy vào VoicePlayer. Cổng riêng, tách khỏi ingest text.
def serve_voice(player, port=VOICE_PLAY_PORT):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                line = raw.decode("utf-8").strip()
                if not line: continue
                try:
                    d = json.loads(line)
                    if d.get("op") == "stop":
                        player.cancel(d["turn_id"])
                    else:
                        player.enqueue(d["turn_id"], d["seq"], d["wav_path"], d["is_last"])
                except Exception as e:
                    _log.info(f"bad play event: {e}")
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), H) as s:
        print(f"voice play server on {port}"); s.serve_forever()