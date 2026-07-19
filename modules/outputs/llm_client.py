"""
llm_client.py — [BƯỚC A] Client gọi LLM thật qua llama-server (llama.cpp).

CHƯA HOÀN THIỆN — stub định hình interface. Sẽ code đầy đủ ở Bước A.

Interface phải KHỚP MockLLM để orchestrator không đổi:
    async generate(req: SpeakRequest) -> async iterator[str]   # yield từng CÂU

Ghi chú triển khai (Bước A):
  - llama-server chạy ở http://localhost:8000/v1 (OpenAI-compatible),
    xem docs/setup_llamacpp.md để dựng server.
  - Dùng client `openai` trỏ tới base_url đó (stream=True) — API giống
    hệt vLLM nên code này TÁI SỬ DỤNG được nếu sau này đổi sang vLLM/AWQ.
  - Gom token thành CÂU (gặp . ! ? \n) rồi mới yield -> sentence streaming.
  - Strip block <think>...</think> nếu model lỡ sinh ra (Qwen3.5 có thinking;
    đã tắt qua --reasoning-format none, nhưng vẫn nên strip phòng hờ).
  - Nạp system prompt từ persona.build_system_prompt().
  - Ghép memory (Bước E) + context chat/monologue vào messages.
"""
from __future__ import annotations
import re
from core.events import SpeakRequest, SpeakSource

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SENTENCE_END = re.compile(r"([.!?…]|\n)")


class LLMClient:
    def __init__(self, base_url: str, model: str, system_prompt: str,
                 max_tokens: int = 200, temperature: float = 0.85,
                 frequency_penalty: float = 0.06) -> None:
        self.base_url = base_url
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.frequency_penalty = frequency_penalty
        # TODO(BƯỚC A): from openai import AsyncOpenAI
        #   self._client = AsyncOpenAI(base_url=base_url, api_key="not-needed")

    def _build_messages(self, req: SpeakRequest) -> list[dict]:
        """Ghép system prompt + ngữ cảnh (chat hoặc monologue) thành messages."""
        messages = [{"role": "system", "content": self.system_prompt}]
        if req.source == SpeakSource.CHAT_REPLY:
            chat = req.context.get("chat", {})
            user_msg = f"[{chat.get('user','?')} nói]: {chat.get('text','')}"
            messages.append({"role": "user", "content": user_msg})
        elif req.source == SpeakSource.MONOLOGUE:
            topic = req.context.get("topic", "")
            phase = req.context.get("phase", "develop")
            messages.append({"role": "user", "content":
                f"(Tự nói, phase={phase}) Hãy tám chuyện về: {topic}"})
        else:
            messages.append({"role": "user", "content": "(lấp khoảng lặng ngắn)"})
        return messages

    async def generate(self, req: SpeakRequest):
        """
        TODO(BƯỚC A):
          1. messages = self._build_messages(req)
          2. gọi self._client.chat.completions.create(..., stream=True)
          3. gom token -> khi gặp _SENTENCE_END thì strip <think> rồi yield câu
          4. cuối cùng yield phần dư nếu có
        """
        raise NotImplementedError("Sẽ hoàn thiện ở Bước A")
        yield  # để Python coi đây là async generator


def strip_think(text: str) -> str:
    """Gỡ mọi block <think>...</think> khỏi text (phòng hờ dù đã tắt ở server)."""
    return _THINK_BLOCK.sub("", text).strip()
