from shared.utils.logger import get_logger
_log = get_logger("memory")

# Tóm tắt bằng LLM; lỗi/bận -> fallback cắt-bớt (giữ tin cuối). KHÔNG chặn.
def summarize(prev_summary: str, old_lines: list, llm) -> str:
    joined = "\n".join(old_lines)
    if llm is None:
        return _truncate_fallback(prev_summary, old_lines)
    try:
        msgs = [{"role": "system", "content":
                 "Tóm tắt hội thoại sau thành 2-3 câu tiếng Việt ngắn gọn, "
                 "giữ thông tin quan trọng về người xem và chủ đề đã nói. Chỉ trả tóm tắt."},
                {"role": "user", "content":
                 (f"Tóm tắt cũ: {prev_summary}\n\n" if prev_summary else "") +
                 f"Hội thoại mới cần gộp:\n{joined}"}]
        out = "".join(llm.stream(msgs, lambda: False)).strip()
        return out or _truncate_fallback(prev_summary, old_lines)
    except Exception as e:
        _log.info(f"summarize LLM fail -> fallback cắt bớt: {e}")
        return _truncate_fallback(prev_summary, old_lines)

def _truncate_fallback(prev_summary: str, old_lines: list) -> str:
    # Fallback: giữ tóm tắt cũ + vài dòng cuối (mất chi tiết nhưng không nghẽn)
    tail = " | ".join(old_lines[-3:])
    return (prev_summary + " | " + tail).strip(" |")[:500]