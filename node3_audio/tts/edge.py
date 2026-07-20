import asyncio, io
import edge_tts
from shared.utils.logger import get_logger
from ..config import TTS_VOICE

_log = get_logger("tts")


class EdgeTTSProvider:
    def __init__(self, voice=TTS_VOICE):
        self._voice = voice

    def synth(self, text: str, wav_path: str) -> float:
        text = (text or "").strip()
        if not text:
            _log.info("skip empty text")
            return 0.0
        try:
            mp3 = asyncio.run(self._synth_bytes(text))
            if not mp3:
                _log.info(f"no audio: {text[:40]!r}")
                return 0.0
            return _mp3_bytes_to_wav(mp3, wav_path)
        except Exception as e:
            _log.info(f"tts fail: {e}")
            return 0.0

    async def _synth_bytes(self, text: str) -> bytes:
        buf = bytearray()
        async for c in edge_tts.Communicate(text, self._voice).stream():
            if c["type"] == "audio":
                buf += c["data"]
        return bytes(buf)


def _mp3_bytes_to_wav(mp3: bytes, wav_path: str) -> float:
    from pydub import AudioSegment
    seg = AudioSegment.from_file(io.BytesIO(mp3), format="mp3")
    seg.export(wav_path, format="wav")
    return len(seg)  # duration_ms
