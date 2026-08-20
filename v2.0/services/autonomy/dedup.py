"""Token-overlap post-check for repeated generated material.

Không dùng embedding — token Jaccard overlap đủ cho MVP (N1 YAGNI).
Composer (Aut.C) gọi is_too_similar sau generate; nếu True → regenerate 1 lần,
fail-open nếu vẫn giống (N7).
"""
from __future__ import annotations

import re
from collections import deque


_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TRAILING_BLOCK_RE = re.compile(r"\s*\[[^\[\]\r\n]*\]\s*$", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    # Bỏ mood block cuối câu để chỉ dedup theo phần Mai nói. Không split ở
    # dấu "[" đầu tiên vì prefix như "[MÔ PHỎNG]" vẫn là phần text cần so.
    head = _TRAILING_BLOCK_RE.sub("", text).lower()
    return set(_WORD_RE.findall(head))


def is_too_similar(new_text: str, recent_texts: list[str], threshold: float = 0.6) -> bool:
    """Jaccard overlap > threshold ở bất kỳ câu gần nhất → True."""
    new_tokens = _tokenize(new_text)
    if not new_tokens:
        return False
    for old in recent_texts:
        old_tokens = _tokenize(old)
        if not old_tokens:
            continue
        union = new_tokens | old_tokens
        if not union:
            continue
        overlap = len(new_tokens & old_tokens) / len(union)
        if overlap > threshold:
            return True
    return False


class DedupBuffer:
    """Buffer lịch sử N câu Mai tự nói gần nhất — composer push vào sau generate."""

    def __init__(self, window: int = 5, threshold: float = 0.6) -> None:
        self.window = max(1, window)
        self.threshold = float(threshold)
        self._recent: deque[str] = deque(maxlen=self.window)

    def check(self, new_text: str) -> bool:
        """Trả True nếu new_text quá giống bất kỳ câu trong buffer."""
        return is_too_similar(new_text, list(self._recent), self.threshold)

    def record(self, text: str) -> None:
        if text and text.strip():
            self._recent.append(text)

    def recent(self) -> list[str]:
        return list(self._recent)
