"""
tts_engine.py — [BƯỚC B] TTS thật (GPT-SoVITS hoặc Kokoro) + producer-consumer.

CHƯA HOÀN THIỆN — stub định hình interface. Sẽ code đầy đủ ở Bước B.

Interface phải KHỚP MockTTS:
    async speak(text: str) -> None
    async stop() -> None

Ghi chú triển khai (Bước B) — đây là phần quyết định ĐỘ MƯỢT:
  - PRODUCER-CONSUMER đa luồng: một luồng tổng hợp audio (nền), một luồng phát.
  - Đang phát câu i thì đã tổng hợp câu i+1 (không đứng chờ).
  - Text normalization TRƯỚC khi synth: bỏ [emotion:X] (đã tách ở emotion.py),
    đọc số thành chữ, bỏ emoji, xử lý viết tắt.
  - stop() phải cắt được giữa chừng (cho barge-in) — xoá hàng đợi audio.
  - first-packet < 200ms (warm-up model trước khi live).
  - Audio ra: virtual audio cable -> VTube Studio (lip-sync) + OBS.
"""
from __future__ import annotations
import asyncio


class TTSEngine:
    def __init__(self, backend: str = "gptsovits") -> None:
        self.backend = backend
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stopped = False
        # TODO(BƯỚC B): load model, khởi động luồng producer-consumer

    async def speak(self, text: str) -> None:
        # TODO(BƯỚC B): normalize -> synth -> đẩy vào queue -> consumer phát
        raise NotImplementedError("Sẽ hoàn thiện ở Bước B")

    async def stop(self) -> None:
        # TODO(BƯỚC B): set cờ dừng + xoá hàng đợi audio (barge-in)
        self._stopped = True
