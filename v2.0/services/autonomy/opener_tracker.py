"""OpenerTracker preventing repeated opening phrases.

Track N câu tự nói gần nhất → trích 3 từ mở đầu mỗi câu → format thành
forbidden_list bơm vào prompt như constraint tường minh.

Chỗ rẻ nhất nhưng hiệu quả nhất — model bị chặn không lặp câu mở, thay vì
hy vọng nó "tự nhiên đa dạng".
"""
from __future__ import annotations

from collections import deque


class OpenerTracker:
    def __init__(self, window: int = 5, words_per_opener: int = 3) -> None:
        self.window = max(1, window)
        self.words = max(1, words_per_opener)
        self._recent: deque[str] = deque(maxlen=self.window)

    def record(self, text: str) -> None:
        """Trích N từ đầu, lưu vào deque. Bỏ qua text rỗng."""
        if not text or not text.strip():
            return
        # Chỉ lấy phần trước dấu [ (Mai xuất mood block cuối câu, không tính là opener)
        head = text.split("[", 1)[0].strip()
        if not head:
            head = text.strip()
        tokens = head.split()[: self.words]
        if not tokens:
            return
        self._recent.append(" ".join(tokens).lower())

    def recent(self) -> list[str]:
        return list(self._recent)

    def forbidden_list(self) -> str:
        """Chuỗi format cho prompt: `"opener1...", "opener2..."` hoặc `(không có)`."""
        if not self._recent:
            return "(không có)"
        return ", ".join(f'"{o}..."' for o in self._recent)

    def reset(self) -> None:
        self._recent.clear()
