import subprocess, wave
from ..config import PIPER_BIN, PIPER_MODEL

# Offline, không cần mạng. Cần model vi .onnx. Khung sẵn, bật khi cần thoát edge-tts.
class PiperTTSProvider:
    def __init__(self, model=PIPER_MODEL, binary=PIPER_BIN):
        self._model, self._bin = model, binary
    def synth(self, text: str, wav_path: str) -> float:
        subprocess.run([self._bin, "-m", self._model, "-f", wav_path],
                       input=text.encode(), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with wave.open(wav_path) as w:
            return w.getnframes() / w.getframerate() * 1000