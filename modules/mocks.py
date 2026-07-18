"""
mocks.py — Bản GIẢ của LLM, TTS, Discord để test khung chạy được ngay
mà chưa cần cài model/CUDA. Khi có đồ thật, thay từng cái bằng bản thật
có CÙNG interface -> không phải sửa orchestrator.

Interface bắt buộc:
  LLM:  async generate(req: SpeakRequest) -> async iterator[str]  (yield từng CÂU)
  TTS:  async speak(text: str) -> None ;  async stop() -> None
"""
from __future__ import annotations
import asyncio
import random

from core.events import SpeakRequest, SpeakSource


class MockLLM:
    """Giả lập LLM: sinh 1-3 'câu' theo kiểu tự nói / trả lời chat."""

    async def generate(self, req: SpeakRequest):
        # giả lập độ trễ 'nghĩ' của model (token đầu)
        await asyncio.sleep(random.uniform(0.15, 0.35))

        if req.source == SpeakSource.CHAT_REPLY:
            chat = req.context.get("chat", {})
            user = chat.get("user", "bạn")
            text = chat.get("text", "")
            sentences = [
                f"Ờ {user} vừa hỏi '{text[:30]}' à.",
                "Để tao nghĩ xem nào...",
                "Câu đó hay đấy, tao thích.",
            ]
        elif req.source == SpeakSource.MONOLOGUE:
            topic = req.context.get("topic", "chuyện vu vơ")
            phase = req.context.get("phase", "develop")
            prefix = {
                "start": "À mà nói này,",
                "develop": "Rồi thì,",
                "transition": "Thôi đổi chuyện,",
            }.get(phase, "")
            sentences = [
                f"{prefix} {topic}.".strip(),
                "Chúng mày thấy sao?",
            ]
        else:  # FILLER / EVENT
            sentences = ["Hừm...", "Chat đâu rồi nhỉ."]

        for s in sentences:
            # giả lập tốc độ sinh câu
            await asyncio.sleep(random.uniform(0.05, 0.15))
            yield s


class MockTTS:
    """Giả lập TTS: 'phát' câu bằng cách in ra + ngủ theo độ dài câu."""

    def __init__(self) -> None:
        self._stopped = False

    async def speak(self, text: str) -> None:
        self._stopped = False
        # first-packet latency giả
        await asyncio.sleep(0.08)
        # thời lượng 'phát' tỉ lệ độ dài (giả lập)
        dur = min(0.4 + len(text) * 0.03, 3.0)
        print(f"   🔊 [TTS] {text}")
        # phát theo từng nhịp nhỏ để có thể bị cancel giữa chừng (barge-in)
        elapsed = 0.0
        step = 0.1
        while elapsed < dur and not self._stopped:
            await asyncio.sleep(step)
            elapsed += step

    async def stop(self) -> None:
        self._stopped = True
        print("   ⏹  [TTS] (ngắt phát)")
