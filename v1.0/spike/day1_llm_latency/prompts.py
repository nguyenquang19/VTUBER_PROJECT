"""Prompt generation cho benchmark Day 1.

Dùng `/tokenize` của llama-server để đo chính xác số token, iterate pad
tới gần target. Tránh overflow context (Gemma+VN tokenizer ratio khác).
"""
from __future__ import annotations

import httpx

SYSTEM_PROMPT = (
    "Bạn là Mai, một AI VTuber tiếng Việt. Trả lời ngắn gọn, tự nhiên, "
    "có cảm xúc. Không thao túng cảm xúc người xem, không giả vờ có ký ức "
    "về đời thực. Nếu không biết, thành thật nói không biết."
)

FILLER = (
    "Hôm nay trời trong xanh và mát mẻ, gió thổi nhè nhẹ qua tán cây phượng. "
    "Buổi sáng thức dậy nghe tiếng chim hót, tự dưng thấy nhẹ nhõm. "
    "Ly cà phê nóng bốc khói trên bàn, mùi thơm quen thuộc lan khắp phòng. "
    "Sách vở còn mở dở, chữ nghĩa như đang chờ ai đó đọc tiếp. "
)

USER_QUESTION = "Bạn nghĩ gì về việc bắt đầu học một ngôn ngữ mới lúc này?"

CONTEXT_PREFIX = "Bối cảnh cuộc trò chuyện với người xem: "


async def count_tokens(client: httpx.AsyncClient, endpoint: str, text: str) -> int:
    r = await client.post(
        f"{endpoint}/tokenize", json={"content": text}, timeout=30.0
    )
    r.raise_for_status()
    return len(r.json().get("tokens", []))


async def build_messages(
    client: httpx.AsyncClient,
    endpoint: str,
    target_prompt_tokens: int,
    tolerance: int = 30,
) -> tuple[list[dict], int]:
    """Sinh messages sao cho prompt_tokens gần target (±tolerance).

    Trả về (messages, measured_prompt_tokens).
    """
    system = SYSTEM_PROMPT
    question = USER_QUESTION
    context = CONTEXT_PREFIX

    # đo overhead (system + question + wrapper chat template không tính được từ /tokenize
    # nên ta đo full combined content mỗi lần và điều chỉnh)
    def _full(ctx: str) -> str:
        return f"{system}\n\n{ctx}\n\n{question}"

    tokens = await count_tokens(client, endpoint, _full(context))

    # thêm filler đến khi gần target
    max_iters = 50
    for _ in range(max_iters):
        if tokens >= target_prompt_tokens - tolerance:
            break
        remaining = target_prompt_tokens - tokens
        # conservative: filler ~ 1 char/token cho VN → nhân đôi cho an toàn
        chars_needed = remaining * 2
        reps = max(1, chars_needed // len(FILLER))
        context += FILLER * reps
        tokens = await count_tokens(client, endpoint, _full(context))

    # cắt bớt nếu vượt quá tolerance
    trim_iters = 30
    while tokens > target_prompt_tokens + tolerance and len(context) > len(CONTEXT_PREFIX) + 50 and trim_iters > 0:
        # cắt 100 char cuối
        context = context[:-100]
        tokens = await count_tokens(client, endpoint, _full(context))
        trim_iters -= 1

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{context}\n\n{question}"},
    ]
    return messages, tokens
