# Duyệt tóm tắt trước khi ghi hồ sơ. Chặn nội dung không phù hợp.
_BLOCK = ("<mẫu cấm>",)   # đội ngũ tự điền
def moderate_summary(text: str) -> tuple[bool, str]:
    low = text.lower()
    for b in _BLOCK:
        if b in low: return False, "chứa nội dung cấm"
    return True, ""