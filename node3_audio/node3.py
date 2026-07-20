from shared.contracts.payloads import SentenceOut
from shared.contracts.timing import TimingTrace
from shared.utils.timing_recorder import stamp, log_trace
from shared.utils.clock import mono_ms
from shared.utils.logger import get_logger
from .config import TTS_ENGINE
from .tts.edge import EdgeTTSProvider
from .player import FilePlayer
from .subtitle import VttWriter
from .cancel import CancelRegistry

_log = get_logger("node3")

def _make_tts():
    if TTS_ENGINE == "piper":
        from .tts.piper import PiperTTSProvider; return PiperTTSProvider()
    return EdgeTTSProvider()

class Node3:
    def __init__(self, tts=None, player=None, cancel=None):
        self.tts = tts or _make_tts()
        self.player = player or FilePlayer()
        self.cancel = cancel or CancelRegistry()
        self._vtt: dict[str, VttWriter] = {}
        self._first_audio_started: set[str] = set()

    def stop(self, turn_id: str):
        self.cancel.cancel(turn_id)      # dừng ngay, bất kể đang ở đâu trong turn

    def on_sentence(self, s: SentenceOut, trace: TimingTrace | None = None):
        if trace is not None: stamp(trace, "t8_node3_received")  # tuyệt đối (so Node2)
        # Hủy được kiểm ngay đầu mỗi câu -> câu tiếp theo của turn bị hủy sẽ không phát.
        if self.cancel.is_cancelled(s.turn_id):
            _log.info(f"skip cancelled turn={s.turn_id} seq={s.seq}"); return

        if s.turn_id not in self._vtt:
            self._vtt[s.turn_id] = VttWriter(self.player.vtt_path(s.turn_id))

        wav = self.player.wav_path(s.turn_id, s.seq)
        # t9: lúc bắt đầu tổng hợp/phát âm thanh thật của câu ĐẦU turn.
        if trace is not None and s.turn_id not in self._first_audio_started:
            stamp(trace, "t9_audio_start"); self._first_audio_started.add(s.turn_id)

        # Kiểm hủy lần nữa ngay trước khi tốn công synth (turn dài, stop tới giữa chừng).
        if self.cancel.is_cancelled(s.turn_id):
            _log.info(f"abort mid-turn={s.turn_id} seq={s.seq}"); return

        dur = self.tts.synth(s.text, wav)
        self._vtt[s.turn_id].add(s.text, dur)
        _log.info(f"audio turn={s.turn_id} seq={s.seq} dur={dur:.0f}ms -> {wav}")


        # MỚI: báo Node1 phát file .wav này vào Discord voice
        self._send_play(s.turn_id, s.seq, wav, s.is_last)

        if s.is_last and trace is not None:
            from shared.utils.trace_sink import emit_partial
            emit_partial("node3", trace)
            log_trace(trace)  # chốt chain khi câu cuối xong (nếu trace đi kèm)
    
    def _send_play(self, turn_id, seq, wav_path, is_last):
        import json, socket, os
        host = os.getenv("VOICE_PLAY_HOST", "127.0.0.1")
        port = int(os.getenv("VOICE_PLAY_PORT", "8806"))
        try:
            ev = {"turn_id": turn_id, "seq": seq,
                  "wav_path": os.path.abspath(wav_path), "is_last": is_last}
            with socket.create_connection((host, port), timeout=1) as s:
                s.sendall((json.dumps(ev) + "\n").encode("utf-8"))
        except Exception as e:
            _log.info(f"gửi play-event fail: {e}")

    def stop(self, turn_id: str):
        self.cancel.cancel(turn_id)
        # báo Node1 dừng phát voice cho turn này
        import json, socket, os
        try:
            with socket.create_connection(
                (os.getenv("VOICE_PLAY_HOST","127.0.0.1"),
                 int(os.getenv("VOICE_PLAY_PORT","8806"))), timeout=1) as s:
                s.sendall((json.dumps({"op":"stop","turn_id":turn_id})+"\n").encode())
        except Exception: pass
