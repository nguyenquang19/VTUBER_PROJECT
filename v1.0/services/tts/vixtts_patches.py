"""Runtime patches cho viXTTS + coqui-tts (Phase 4 4.A).

Coqui-tts stock KHÔNG có VN tokenizer + torch 2.11 KHÔNG tương thích torchcodec+
FFmpeg 8. Ta patch 2 điểm để chạy viXTTS trên môi trường hiện tại:

1) `torchaudio.load` → soundfile (tránh torchcodec)
2) `VoiceBpeTokenizer.preprocess_text(lang='vi')` → dùng VN cleaner (expand số
   qua `num2words`, normalize whitespace, lowercase)

Cleaner + expander là hàm THUẦN (không side effect) — test được không cần apply
patches. `apply_patches()` idempotent — gọi nhiều lần chỉ có tác dụng lần đầu.
"""
from __future__ import annotations

import re
from typing import Any

# --- pure helpers (test được không cần patch) ---

_NUM_RE = re.compile(r"\d[\d.,]*")
_WS_RE = re.compile(r"\s+")


def vi_expand_numbers(text: str) -> str:
    """`1.250.000` → `một triệu hai trăm năm mươi nghìn` (nếu num2words available).

    Nếu num2words không nhận diện được → giữ nguyên chuỗi số gốc (fail-safe).
    """
    try:
        from num2words import num2words as _n2w
    except ImportError:
        return text

    def _repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        digits = raw.replace(".", "").replace(",", "")
        try:
            return _n2w(int(digits), lang="vi")
        except Exception:
            return raw

    return _NUM_RE.sub(_repl, text)


def vi_clean(text: str) -> str:
    """VN cleaner cho tokenizer viXTTS: expand số + collapse whitespace + lowercase."""
    text = vi_expand_numbers(text)
    text = _WS_RE.sub(" ", text).strip()
    return text.lower()


# --- runtime patches (side-effectful, chỉ apply khi gọi apply_patches) ---

_PATCHED = False


def _patch_torchaudio_load() -> None:
    """torchaudio 2.11 dùng torchcodec → FFmpeg 8 incompat. Dùng soundfile thay."""
    import torch as _t
    import torchaudio
    import soundfile as _sf

    def _sf_load(path: Any, *_args: Any, **_kw: Any):
        audio, sr = _sf.read(str(path), dtype="float32", always_2d=True)
        return _t.from_numpy(audio.T), sr

    torchaudio.load = _sf_load  # type: ignore[assignment]


def _patch_tokenizer_vi() -> None:
    """Bọc VoiceBpeTokenizer.preprocess_text để lang='vi' đi qua vi_clean."""
    from TTS.tts.layers.xtts import tokenizer as _tk

    orig = _tk.VoiceBpeTokenizer.preprocess_text

    def _wrapped(self: Any, txt: str, lang: str) -> str:  # noqa: N802 (match method sig)
        if lang == "vi":
            return vi_clean(txt)
        return orig(self, txt, lang)

    _tk.VoiceBpeTokenizer.preprocess_text = _wrapped  # type: ignore[assignment]


def apply_patches() -> bool:
    """Áp cả 2 patch. Idempotent — trả True nếu vừa apply, False nếu đã apply trước."""
    global _PATCHED
    if _PATCHED:
        return False
    _patch_torchaudio_load()
    _patch_tokenizer_vi()
    _PATCHED = True
    return True


def is_patched() -> bool:
    return _PATCHED
