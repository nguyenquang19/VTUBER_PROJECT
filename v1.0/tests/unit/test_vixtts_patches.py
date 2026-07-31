"""Test VN cleaner + patch idempotence (Phase 4, 4.A).

Cleaner + expander là hàm THUẦN — test không cần apply patch (không load coqui-tts
model 1.79GB). apply_patches có test riêng đảm bảo idempotent."""
from __future__ import annotations

import pytest

from services.tts.vixtts_patches import (
    apply_patches,
    is_patched,
    vi_clean,
    vi_expand_numbers,
)


class TestExpandNumbers:
    def test_simple_int(self) -> None:
        # num2words vi: 5 -> "năm"
        out = vi_expand_numbers("5 con mèo")
        assert "năm" in out
        assert "5" not in out

    def test_dotted_thousand(self) -> None:
        # 1.250.000 -> "một triệu hai trăm năm mươi nghìn" (không assert từng chữ,
        # chỉ chắc là ĐÃ expand + không còn digits)
        out = vi_expand_numbers("Giá 1.250.000 đồng")
        assert "1.250.000" not in out
        assert "1250000" not in out
        assert "triệu" in out

    def test_multiple_numbers(self) -> None:
        out = vi_expand_numbers("có 3 con và 5 quyển")
        assert "3" not in out and "5" not in out

    def test_no_number_unchanged(self) -> None:
        assert vi_expand_numbers("chào cậu") == "chào cậu"

    def test_empty(self) -> None:
        assert vi_expand_numbers("") == ""


class TestClean:
    def test_lowercase(self) -> None:
        assert vi_clean("CHÀO Cậu") == "chào cậu"

    def test_collapses_whitespace(self) -> None:
        assert vi_clean("chào    cậu\n\ncó\tkhoẻ  không") == "chào cậu có khoẻ không"

    def test_expands_and_cleans(self) -> None:
        out = vi_clean("Giá 5000 ĐỒNG")
        assert "5000" not in out
        assert out == out.lower()
        assert "  " not in out

    def test_strips(self) -> None:
        assert vi_clean("   xin chào   ") == "xin chào"

    def test_empty(self) -> None:
        assert vi_clean("") == ""


class TestApplyPatches:
    def test_apply_idempotent(self) -> None:
        # 1st call may or may not have run (depends on prior tests) — just check
        # 2nd back-to-back returns False.
        apply_patches()
        assert is_patched() is True
        assert apply_patches() is False        # đã patched → no-op
        assert is_patched() is True

    def test_torchaudio_uses_soundfile_after_patch(self, tmp_path) -> None:
        # apply patches (idempotent — an toàn nếu đã patched)
        apply_patches()

        import soundfile as sf
        import numpy as np
        import torchaudio

        # ghi file WAV 16-bit đơn giản
        p = tmp_path / "t.wav"
        sf.write(str(p), np.zeros(1600, dtype="float32"), 16000)

        audio, sr = torchaudio.load(str(p))
        assert sr == 16000
        assert audio.shape[-1] == 1600
