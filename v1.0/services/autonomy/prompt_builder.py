"""Prompt builder cho Autonomy Engine v2 (Aut.C, spec Mục 2.3 + 2.4 Bước 2).

Render per-category slot-fill prompt. Composer gọi `render_prompt(category,
material, mood, forbidden_openers, hint)` → text để inject vào messages qua
PromptManager (build_request bình thường, không đụng persona cache).

Anti-repeat opener chèn TƯỜNG MINH trong prompt (Bước 3).
"""
from __future__ import annotations

from interfaces.animation import MoodState

_MOOD_DIMS = ("vui", "buon", "buc", "bon_chon", "nguong")


def _mood_str(m: MoodState) -> str:
    return " ".join(f"{d}={getattr(m, d)}" for d in _MOOD_DIMS)


def render_prompt(
    category: str,
    material: dict,
    mood: MoodState,
    forbidden_openers: str,
    prompt_hint: str,
) -> str:
    """Render Vietnamese instruction. Caller inject vào messages[user] hoặc system."""
    body = _render_body(category, material)
    return (
        f"[Context — Mai tự lên tiếng lượt này, KHÔNG phải trả lời chat]\n"
        f"- Lý do: {category}\n"
        f"- Hint: {prompt_hint}\n"
        f"- Mood: {_mood_str(mood)}\n"
        f"{body}"
        f"- KHÔNG được mở đầu bằng: {forbidden_openers}\n"
        f"\n"
        f"Viết theo lý do trên, đúng chất Mai (persona đã dặn). 1-2 câu ngắn.\n"
        f"Không copy nguyên seed/số liệu — diễn đạt lại. Không lặp câu mở đã cấm.\n"
        f"Vẫn xuất mood block cuối câu theo format bắt buộc."
    )


def _render_body(category: str, m: dict) -> str:
    """Phần data-cụ-thể per-category — nhét dữ kiện thật vào slot."""
    if category == "complain_silence":
        return (
            f"- Số liệu thật: đã im lặng {m.get('silence_seconds', 0)}s, "
            f"chat có {m.get('chat_count_10min', 0)} tin trong 10 phút qua.\n"
        )
    if category == "share_thought":
        return f"- Hạt giống chủ đề: \"{m.get('topic_seed', '')}\".\n"
    if category == "ask_chat":
        return (
            f"- Hạt giống câu hỏi ({m.get('question_kind', 'chung')}): "
            f"\"{m.get('question_seed', '')}\".\n"
        )
    if category == "call_operator":
        online = "đang online" if m.get("operator_online") else "chưa thấy"
        return (
            f"- Trạng thái ông: {online}. "
            f"Số lần tự nói mà chưa được reply: {m.get('ignored_streak', 0)}.\n"
        )
    if category == "follow_up_topic":
        return f"- Chủ đề vừa nhắc: {m.get('memory_snippet', '')}.\n"
    return "- (không có material cụ thể)\n"
