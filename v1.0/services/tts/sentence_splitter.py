"""Sentence splitter tiếng Việt cho pipeline TTS (Phase 4 4.C).

TTS xử theo câu (spike day2: chia câu → synthesize → giấu latency câu N+1). Tách
theo dấu ngắt cuối câu (. ! ? …) VÀ giữ nguyên dấu, không cắt trong số thập phân
(3.14) hoặc viết tắt số (1.250.000). VN không có logic phức tạp như Anh (Mr./Dr.);
YAGNI: regex đơn giản đã đủ.
"""
from __future__ import annotations

import re
from typing import Callable

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


# Regex bắt câu hoàn chỉnh (kết bằng dấu kết) — dùng cho LiveSentenceStreamer.
# Khác _SENT_RE ở chỗ: yêu cầu câu KẾT bằng dấu kết (không nuốt trailing text).
_COMPLETE_SENT_RE = re.compile(
    rf"(.*?[{_END_PUNCT.strip('[]')}]+[\"')\]]?\s*)",
    re.DOTALL,
)
# Mood block trong persona luôn có dạng `[vui:N buon:N buc:N bon_chon:N nguong:N]`
# → dùng `[vui:` làm marker (đặc trưng, không xảy ra trong lời nói bình thường).
# Cũng bắt biến thể có/không dấu và whitespace: `[ vui :`, `[Vui:`, ...
_MOOD_BLOCK_START = re.compile(r"\[\s*vui\s*:", re.IGNORECASE)


class LiveSentenceStreamer:
    """Nhận token LLM streaming, phát complete-sentence ra callback ngay lập tức.

    Vai trò: cho phép TTS bắt đầu synth câu 1 khi LLM chưa xuất xong (câu 2, 3, ...).
    Persona luôn xuất mood block sau text (dạng `\\n[vui:...]`) — streamer DỪNG
    nạp khi thấy pattern đó (không đọc mood block vào TTS).

    Không async — callback được gọi sync từ push(). Caller thường schedule
    `asyncio.create_task(pipeline.speak(sent))` bên trong callback.
    """

    def __init__(self, on_sentence: Callable[[str], None], min_len: int = 1) -> None:
        self._on_sentence = on_sentence
        self._buf = ""
        self._min_len = min_len
        self._blocked = False   # bật khi thấy mood block → không nạp thêm

    def push(self, token: str) -> None:
        if self._blocked or not token:
            return
        self._buf += token
        # Cắt ở mood block start nếu có
        m = _MOOD_BLOCK_START.search(self._buf)
        if m:
            head, self._buf = self._buf[: m.start()], ""
            self._blocked = True
            self._emit_from(head, finalize=True)
            return
        # Emit câu hoàn chỉnh, giữ tail chưa hoàn chỉnh trong buf
        self._buf = self._emit_from(self._buf, finalize=False)

    def close(self) -> None:
        """Gọi khi LLM turn xong. Flush tail nếu chưa blocked (câu chưa kết dấu)."""
        if self._blocked:
            self._buf = ""
            return
        self._emit_from(self._buf, finalize=True)
        self._buf = ""

    @property
    def blocked(self) -> bool:
        return self._blocked

    def _emit_from(self, text: str, finalize: bool) -> str:
        """Emit câu hoàn chỉnh từ `text`. Trả về phần dư (buf mới)."""
        rest = text
        for m in _COMPLETE_SENT_RE.finditer(text):
            sent = m.group(1).strip()
            rest = text[m.end():]
            if self._alnum_count(sent) >= self._min_len:
                self._on_sentence(sent)
        if finalize:
            tail = rest.strip()
            if tail and self._alnum_count(tail) >= self._min_len:
                self._on_sentence(tail)
            return ""
        return rest

    @staticmethod
    def _alnum_count(s: str) -> int:
        return sum(1 for ch in s if ch.isalnum())
