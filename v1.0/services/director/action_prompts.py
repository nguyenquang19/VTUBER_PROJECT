"""Pure prompt and history rendering helpers for Director actions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.director.director import ReadMode


def read_user_text(dec: Any) -> str:
    """Return only real chat text for a user turn."""
    refs = dec.refs
    if not refs:
        return ""
    if dec.read_mode == ReadMode.CLUSTER:
        return " / ".join(ref.text for ref in refs[:3])
    return refs[0].text


def proactive_thread_directive(thread: Any) -> str | None:
    if thread is None:
        return None
    move = thread.next_move.value if thread.next_move is not None else "deepen"
    lines = [
        "[Grounded conversation thread]",
        f"Thread ID: {thread.thread_id}",
        f"Topic: {thread.topic}",
        f"Next public move: {move}",
        "Do not repeat an already-said point and do not invent viewer input.",
    ]
    if thread.claims:
        lines.append("Already said: " + " | ".join(item.text for item in thread.claims[-2:]))
    if thread.viewer_contributions:
        lines.append(
            "Viewer evidence: "
            + " | ".join(item.text for item in thread.viewer_contributions[-2:])
        )
    return "\n".join(lines)


def self_talk_correction_prompt(
    original_prompt: str,
    rejected_text: str,
    *,
    max_sentences: int,
    allow_question: bool,
    require_question: bool,
    reasons: tuple[str, ...],
) -> str:
    question_rule = (
        "Phải có đúng một câu hỏi ở cuối."
        if require_question else
        "Không được dùng câu hỏi." if not allow_question else
        "Chỉ hỏi nếu câu hỏi bám trực tiếp vào mạch."
    )
    excerpt = " ".join(str(rejected_text).split())[:320]
    repeat_rule = (
        "Không chép lại hoặc diễn đạt lại bản trước/câu trước; chỉ viết phần ý mới.\n"
        if "stage_repeat" in reasons else ""
    )
    semantic_rule = (
        "Câu hỏi được nhận diện theo nghĩa, kể cả khi đổi dấu '?' thành dấu '.'. "
        "Kết thúc bằng một nhận xét khẳng định; không dùng đuôi hỏi như "
        "nhỉ, hả, à, không, chưa, sao, gì hoặc nào.\n"
        if "question_not_allowed" in reasons else ""
    )
    invite_count_rule = (
        "Xóa các câu hỏi thừa; bản sửa chỉ được giữ đúng một câu hỏi ở cuối.\n"
        if "invitation_question_count" in reasons else ""
    )
    return (
        f"{original_prompt}\n"
        "[SỬA HÌNH DÁNG OUTPUT — chỉ thử lại một lần]\n"
        f"Bản trước không đạt: {', '.join(reasons)}.\n"
        f"Viết lại tối đa {max_sentences} câu. {question_rule}\n"
        f"{repeat_rule}{semantic_rule}{invite_count_rule}"
        "Giữ nguyên mỏ neo và ý định; rút gọn, không giải thích việc sửa.\n"
        f"Bản cần sửa: {excerpt}"
    )


def timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def stage_direction_for(dec: Any) -> str | None:
    refs = dec.refs
    if dec.read_mode == ReadMode.ACK and refs:
        ref = refs[0]
        who = ref.viewer_name or ref.viewer_id or "một người"
        return f"{who} vừa SUPERCHAT (ủng hộ tiền) — ack ngay, cảm ơn tự nhiên đúng giọng Mai."
    if dec.read_mode == ReadMode.CLUSTER:
        return "Mấy người đang hỏi/nói cùng chủ đề — đáp GỘP 1 lần, đừng lặp lại từng câu."
    return None


def join_directives(*values: str | None) -> str:
    return "\n".join(value for value in values if value)


def room_reaction_prompt(dec: Any) -> str:
    if dec.read_mode == ReadMode.VIBE:
        return (
            "[Context — Mai react KHÔNG KHÍ chat, KHÔNG trả lời ai cụ thể]\n"
            "Chat đang bùng, cả đám spam cùng kiểu. React theo VIBE bằng 1 câu ngắn "
            "đúng giọng Mai, KHÔNG đáp lẻ từng người. Chỉ viết thoại."
        )
    return (
        "[Context — Mai react KHÔNG KHÍ chat, KHÔNG trả lời ai cụ thể]\n"
        "Chat trôi nhanh, nhiều tin lặt vặt đọc không kịp. Nói 1 câu tổng kiểu "
        "'chat trôi nhanh quá' đúng giọng Mai, KHÔNG đáp lẻ từng tin. Chỉ viết thoại."
    )


def history_text_for(dec: Any) -> tuple[str | None, bool]:
    refs = dec.refs
    if dec.read_mode in (ReadMode.SUMMARY, ReadMode.VIBE) or not refs:
        return None, False
    if dec.read_mode == ReadMode.CLUSTER:
        joined = " / ".join(ref.text for ref in refs[:3])
        return f"(mấy người cùng hỏi) {joined}", True
    return refs[0].text, True
