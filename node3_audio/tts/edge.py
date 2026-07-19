import asyncio, wave, edge_tts
from ..config import TTS_VOICE

# edge-tts trả mp3 -> decode sang wav PCM để .wav chuẩn + tính duration thật.
class EdgeTTSProvider:
    def __init__(self, voice=TTS_VOICE): self._voice = voice

    def synth(self, text: str, wav_path: str) -> float:
        mp3 = asyncio.run(self._synth_bytes(text))
        return _mp3_bytes_to_wav(mp3, wav_path)

    async def _synth_bytes(self, text: str) -> bytes:
        buf = bytearray()
        async for c in edge_tts.Communicate(text, self._voice).stream():
            if c["type"] == "audio": buf += c["data"]
        return bytes(buf)

def _mp3_bytes_to_wav(mp3: bytes, wav_path: str) -> float:
    from pydub import AudioSegment   # dùng ffmpeg
    import io
    seg = AudioSegment.from_file(io.BytesIO(mp3), format="mp3")
    seg.export(wav_path, format="wav")
    return len(seg)  # duration_ms