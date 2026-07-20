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
        from .tts.piper import PiperTTSProvider
        return PiperTTSProvider()
    return EdgeTTSProvider()


class Node3:
    def __init__(self, tts=None, player=None, cancel=None):
        self.tts = tts or _make_tts()
        self.player = player or FilePlayer()
        self.cancel = cancel or CancelRegistry()
        self._vtt: dict[str, VttWriter] = {}
        self._traces = {}
        self._first_audio_started: set[str] = set()

    def stop(self, turn_id: str):
        self.cancel.cancel(turn_id)
        # báo Node1 dừng phát voice cho turn này
        import json, socket, os
        try:
            with socket.create_connection(
                (os.getenv("VOICE_PLAY_HOST", "127.0.0.1"),
                 int(os.getenv("VOICE_PLAY_PORT", "8806"))), timeout=1) as s:
                s.sendall((json.dumps({"op": "stop", "turn_id": turn_id}) + "\n").encode())
        except Exception:
            pass

    def on_sentence(self, s: SentenceOut, trace: TimingTrace | None = None):
        # Nhận trace ở câu đầu -> stamp t8 + LƯU theo turn_id (dùng lại ở câu cuối)
        import sys; print(f"N3_DBG recv turn={s.turn_id[:8]} seq={s.seq} is_last={s.is_last} has_trace={trace is not None}", file=sys.stderr, flush=True)
        if trace is not None:
            stamp(trace, "t8_node3_received")
            self._traces[s.turn_id] = trace

        # Hủy: câu tiếp theo của turn bị hủy sẽ không phát
        if self.cancel.is_cancelled(s.turn_id):
            _log.info(f"skip cancelled turn={s.turn_id} seq={s.seq}")
            return

        if s.turn_id not in self._vtt:
            self._vtt[s.turn_id] = VttWriter(self.player.vtt_path(s.turn_id))

        wav = self.player.wav_path(s.turn_id, s.seq)

        # t9: stamp vào trace ĐÃ LƯU (không phải trace đi kèm câu này)
        saved = self._traces.get(s.turn_id)
        if saved is not None and s.turn_id not in self._first_audio_started:
            stamp(saved, "t9_audio_start")
            self._first_audio_started.add(s.turn_id)

        # Kiểm hủy lần nữa trước khi tốn công synth
        if self.cancel.is_cancelled(s.turn_id):
            _log.info(f"abort mid-turn={s.turn_id} seq={s.seq}")
            return

        dur = self.tts.synth(s.text, wav)
        self._vtt[s.turn_id].add(s.text, dur)
        _log.info(f"audio turn={s.turn_id} seq={s.seq} dur={dur:.0f}ms -> {wav}")

        # Báo Node1 phát file .wav vào Discord voice
        self._send_play(s.turn_id, s.seq, wav, s.is_last)

        # Câu cuối: lấy trace đã lưu -> emit timing (đủ t8/t9)
        if s.is_last:
            done = self._traces.pop(s.turn_id, None)
            import sys; print(f"N3_DBG last turn={s.turn_id[:8]} done={done is not None}", file=sys.stderr, flush=True)
            self._first_audio_started.discard(s.turn_id)
            if done is not None:
                from shared.utils.trace_sink import emit_partial
                emit_partial("node3", done)
                log_trace(done)

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
