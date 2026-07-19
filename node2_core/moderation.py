_BLOCK = ("<nội dung cấm mẫu>",)  # đội ngũ tự điền; đây chỉ khung
def moderate(text: str) -> tuple[bool, str]:
    low = text.lower()
    if any(b in low for b in _BLOCK):
        return False, ""            # bị chặn
    return True, text