import re
_SENT = re.compile(r'[^.!?…\n]+[.!?…]?', re.UNICODE)

def split_sentences(text: str):
    parts = [s.strip() for s in _SENT.findall(text) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])