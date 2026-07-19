import os
from .config import OUT_DIR

# v1: ghi .wav ra đĩa. Đặt tên theo turn_id + seq để không đè, dễ ráp OBS/Live2D.
class FilePlayer:
    def __init__(self, out_dir=OUT_DIR):
        self._dir = out_dir; os.makedirs(out_dir, exist_ok=True)
    def wav_path(self, turn_id: str, seq: int) -> str:
        return os.path.join(self._dir, f"{turn_id}_{seq:03d}.wav")
    def vtt_path(self, turn_id: str) -> str:
        return os.path.join(self._dir, f"{turn_id}.vtt")