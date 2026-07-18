"""
emotion.py — Tách thẻ [emotion:X] ra khỏi text.

LLM chèn [emotion:happy] vào đầu câu. Ta cần:
  - Lấy emotion X -> gửi avatar (VTube Studio) đổi biểu cảm.
  - GỠ thẻ khỏi text trước khi đưa vào TTS (không đọc "emotion happy").

Trả về (emotion|None, clean_text).
"""
from __future__ import annotations
import re

_PATTERN = re.compile(r"\[emotion:\s*([a-zA-Z_]+)\s*\]", re.IGNORECASE)

# danh sách emotion hợp lệ (khớp system prompt). Emotion lạ -> bỏ qua an toàn.
VALID = {"happy", "laugh", "pout", "surprised", "smug", "shy", "sad", "thinking", "neutral"}


def parse_emotion(text: str) -> tuple[str | None, str]:
    emotion = None
    m = _PATTERN.search(text)
    if m:
        cand = m.group(1).lower()
        if cand in VALID:
            emotion = cand
        # gỡ TẤT CẢ thẻ emotion khỏi text (kể cả thẻ lạ) để TTS không đọc
    clean = _PATTERN.sub("", text).strip()
    # dọn khoảng trắng thừa
    clean = re.sub(r"\s{2,}", " ", clean)
    return emotion, clean
