"""Sentence splitter tiếng Việt cho pipeline TTS (Phase 4 4.C).

TTS xử theo câu (spike day2: chia câu → synthesize → giấu latency câu N+1). Tách
theo dấu ngắt cuối câu (. ! ? …) VÀ giữ nguyên dấu, không cắt trong số thập phân
(3.14) hoặc viết tắt số (1.250.000). VN không có logic phức tạp như Anh (Mr./Dr.);
YAGNI: regex đơn giản đã đủ.
"""
from __future__ import annotations

import re

# dấu kết câu: . ! ? … và cụm ...
_END_PUNCT = r"[.!?…]"
# 1 câu = chuỗi ký tự không chứa dấu kết + (một hoặc nhiều dấu kết + optional
# đóng ngoặc/quote) + optional whitespace. Kèm lookahead để không nuốt dấu.
_SENT_RE = re.compile(
    rf"[^{_END_PUNCT.strip('[]')}]+(?:{_END_PUNCT}+[\"')\]]?\s*)+",
    re.DOTALL,
)
# số thập phân/viết tắt (3.14, 1.250.000) — dấu chấm giữa 2 số không phải kết câu
_NUM_DOT = re.compile(r"(\d)\.(\d)")
_NUM_DOT_PLACEHOLDER = "\x01"


def split_vn(text: str, min_len: int = 1) -> list[str]:
    """Cắt `text` thành list câu (giữ dấu). Câu rỗng bị bỏ.

    `min_len`: tối thiểu số ký tự chữ/số (alnum) trong câu để giữ (mặc định 1).
    Câu chỉ có dấu câu (`". . ."`) sẽ bị lọc.
    """
    if not text or not text.strip():
        return []

    # Bảo vệ số thập phân/viết tắt số khỏi bị split
    protected = _NUM_DOT.sub(rf"\1{_NUM_DOT_PLACEHOLDER}\2", text)
    # Áp lặp lại 1 lần nữa cho "1.250.000" (3 chấm chuỗi)
    protected = _NUM_DOT.sub(rf"\1{_NUM_DOT_PLACEHOLDER}\2", protected)

    parts: list[str] = []
    matches = list(_SENT_RE.finditer(protected))
    covered_end = 0
    for m in matches:
        s = m.group(0)
        parts.append(s)
        covered_end = m.end()
    # Phần đuôi không có dấu kết (câu chưa hoàn chỉnh) — vẫn giữ như 1 câu
    tail = protected[covered_end:]
    if tail.strip():
        parts.append(tail)

    # Khôi phục placeholder + strip + lọc: giữ nếu có >= min_len ký tự alnum
    out = []
    for p in parts:
        s = p.replace(_NUM_DOT_PLACEHOLDER, ".").strip()
        alnum_count = sum(1 for ch in s if ch.isalnum())
        if alnum_count >= min_len:
            out.append(s)
    return out
