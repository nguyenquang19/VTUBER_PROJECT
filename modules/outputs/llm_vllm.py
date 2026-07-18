"""
llm_vllm.py — [BƯỚC A] Client gọi Qwen3.5-9B thật qua vLLM (OpenAI-compatible).

CHƯA HOÀN THIỆN — đây là stub định hình interface. Sẽ code đầy đủ ở Bước A.

Interface phải KHỚP MockLLM để orchestrator không đổi:
    async generate(req: SpeakRequest) -> async iterator[str]   # yield từng CÂU

Ghi chú triển khai (Bước A):
  - vLLM serve Qwen3.5-9B ở http://localhost:8000/v1 (xem docs/setup.md)
  - Dùng client openai trỏ tới endpoint đó (stream=True)
  - TẮT thinking mode (Qwen3.5 có thinking) để hợp hội thoại realtime
  - Gom token thành CÂU (gặp . ! ? \n) rồi mới yield -> sentence streaming
  - Strip block <think>...</think> nếu model lỡ sinh ra
  - Nạp system prompt từ persona.build_system_prompt()
  - Ghép memory (Bước E) + context chat/monologue vào messages
"""
from __future__ import annotations
from core.events import SpeakRequest


class VLLMClient:
    def __init__(self, base_url: str, model: str, system_prompt: str) -> None:
        self.base_url = base_url
        self.model = model
        self.system_prompt = system_prompt
        # TODO(BƯỚC A): khởi tạo AsyncOpenAI(base_url=..., api_key="EMPTY")

    async def generate(self, req: SpeakRequest):
        # TODO(BƯỚC A): build messages từ system_prompt + req.context
        #               stream token, gom thành câu, strip <think>, yield từng câu
        raise NotImplementedError("Sẽ hoàn thiện ở Bước A")
        yield  # để Python coi đây là async generator
