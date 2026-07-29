"""Prompt generation cho benchmark Day 1.

Sinh prompt tiếng Việt ở kích thước target (500 / 2000 / 4000 tokens xấp xỉ).
Số token thực tế do llama-server trả về trong `usage.prompt_tokens`.
"""
from __future__ import annotations

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

# xấp xỉ tokenizer Gemma cho tiếng Việt (đo bằng /tokenize sẽ chính xác hơn)
CHARS_PER_TOKEN = 3.5


def _pad(prefix: str, target_tokens: int) -> str:
    target_chars = int(target_tokens * CHARS_PER_TOKEN)
    if len(prefix) >= target_chars:
        return prefix[:target_chars]
    reps = (target_chars - len(prefix)) // len(FILLER) + 1
    return prefix + (FILLER * reps)[: target_chars - len(prefix)]


def make_prompt(target_tokens: int) -> list[dict]:
    system_budget = 60
    question_budget = 25
    context_budget = max(0, target_tokens - system_budget - question_budget)
    context = _pad("Bối cảnh cuộc trò chuyện với người xem: ", context_budget)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{context}\n\n{USER_QUESTION}"},
    ]
