from dataclasses import dataclass
from typing import Protocol

@dataclass
class AudioResult:
    turn_id: str
    seq: int
    wav_path: str
    duration_ms: float
    text: str

# Bọc engine TTS. Đổi engine = viết thêm 1 adapter, không đụng Node3 còn lại.
class TTSProvider(Protocol):
    def synth(self, text: str, wav_path: str) -> float: ...  # trả duration_ms