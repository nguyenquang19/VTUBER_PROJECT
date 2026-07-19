import re

# Xóa control/special token rò rỉ từ model (harmony channel, chatml, v.v.)
_CTRL = re.compile(r'<\|?/?(channel|start|end|message|im_start|im_end|assistant|user|system)[^>]*\|?>',
                   re.IGNORECASE)
_CHANNEL_WORDS = re.compile(r'\b(thought|analysis|final)\b\s*[:>]?', re.IGNORECASE)

def sanitize(text: str) -> str:
    text = _CTRL.sub("", text)
    # bỏ nhãn kênh còn sót ở đầu dòng
    text = re.sub(r'^\s*(thought|analysis|assistant|ai)\s*[:>]\s*', '', text,
                  flags=re.IGNORECASE | re.MULTILINE)
    return text.strip()

_BLOCK = ("<nội dung cấm mẫu>",)
def moderate(text: str) -> tuple[bool, str]:
    text = sanitize(text)            # làm sạch TRƯỚC khi xét
    low = text.lower()
    if any(b in low for b in _BLOCK):
        return False, ""
    return True, text