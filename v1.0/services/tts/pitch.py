"""Pitch-shift cho audio đầu ra TTS (thêm ở v1.0.3).

Đổi cao độ mảng float32 mono NHƯNG giữ nguyên độ dài (không đổi tốc độ), qua
phase-vocoder của librosa. `semitones` dương → giọng cao/trẻ hơn, âm → trầm hơn.
`semitones == 0.0` → no-op: trả nguyên mảng, không import librosa, không tốn CPU
→ mặc định baseline không đổi hành vi. Dùng trong `AudioPlayer` trước khi phát.
"""
from __future__ import annotations

import numpy as np

# Ngoài vùng này pitch-shift méo giọng nặng — clamp để config sai không phá tiếng.
_MIN_SEMITONES = -12.0
_MAX_SEMITONES = 12.0


def clamp_semitones(semitones: float) -> float:
    """Giới hạn semitones về [-12, 12]."""
    return max(_MIN_SEMITONES, min(_MAX_SEMITONES, float(semitones)))


def pitch_shift_samples(
    samples: np.ndarray, sample_rate: int, semitones: float,
) -> np.ndarray:
    """Đổi cao độ float32 mono theo semitone. `0.0` hoặc mảng rỗng → trả nguyên."""
    steps = clamp_semitones(semitones)
    if steps == 0.0 or samples.size == 0:
        return samples
    import librosa

    shifted = librosa.effects.pitch_shift(
        samples.astype(np.float32), sr=sample_rate, n_steps=steps,
    )
    return shifted.astype(np.float32)
