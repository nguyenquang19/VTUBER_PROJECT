import threading
from collections import defaultdict, deque
from .summarizer import summarize
from ..config import CTX_BUDGET_CHARS

# Tầng 1: lịch sử thô gần đây. Tầng 2: tóm tắt đầu buổi.
# Bộ điều phối CHỈ gọi 3 hàm: context / append / maybe_summarize.
# Đổi thuật toán tóm tắt sau này KHÔNG đụng bộ điều phối.
class SessionMemory:
    def __init__(self, budget=CTX_BUDGET_CHARS, llm=None):
        self._budget = budget
        self._llm = llm
        self._recent = defaultdict(deque)      # session -> deque[str] (tin gần đây)
        self._summary = defaultdict(str)       # session -> tóm tắt đầu buổi
        self._lock = threading.Lock()

    # (1) Lấy ngữ cảnh sẵn sàng đưa vào prompt
    def context(self, session: str) -> str:
        with self._lock:
            parts = []
            if self._summary[session]:
                parts.append(f"[Tóm tắt trước đó] {self._summary[session]}")
            parts.extend(self._recent[session])
            return "\n".join(parts)

    # (1b) Ngữ cảnh gói trong max_chars — Phần 6. Giữ tóm tắt (ưu tiên cao) +
    # các lượt MỚI NHẤT, cắt lượt CŨ trước. KHÔNG bao giờ trả vượt max_chars.
    def context_within(self, session: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        with self._lock:
            summary = self._summary[session]
            recent = list(self._recent[session])
        parts = []
        used = 0
        if summary:
            s = f"[Tóm tắt trước đó] {summary}"
            if len(s) > max_chars:
                s = s[:max_chars]          # tóm tắt tự nó đã quá dài -> cắt
            parts.append(s)
            used = len(s)
        kept = []
        for line in reversed(recent):      # lượt mới nhất trước
            add = len(line) + 1            # +1 xấp xỉ ký tự xuống dòng nối
            if used + add > max_chars:
                break                       # hết chỗ -> bỏ các lượt cũ hơn
            kept.append(line)
            used += add
        kept.reverse()                      # trả về đúng thứ tự thời gian
        parts.extend(kept)
        return "\n".join(parts)

    # (2) Ghi thêm 1 lượt hội thoại
    def append(self, session: str, line: str):
        with self._lock:
            self._recent[session].append(line)

    # (3) Tự kiểm ngưỡng (~55% budget) và tóm tắt nền khi cần.
    # Hạ từ 70% xuống 55% (Phần 6): giờ mood + hồ sơ + dòng nhắc cũng chiếm chỗ,
    # tóm tắt sớm hơn để lịch sử không đẩy tổng prompt vượt context.
    def maybe_summarize(self, session: str):
        with self._lock:
            recent = self._recent[session]
            size = len(self._summary[session]) + sum(len(x) for x in recent)
            if size < self._budget * 0.55:
                return                          # chưa tới ngưỡng
            # tách phần cũ để tóm tắt, giữ vài tin gần nhất nguyên văn
            keep = 4
            old = list(recent)[:-keep] if len(recent) > keep else []
            if not old:
                return
        # tóm tắt Ở NGOÀI lock (có thể gọi LLM chậm) -> chạy nền
        threading.Thread(target=self._do_summarize,
                         args=(session, old, keep), daemon=True).start()

    def _do_summarize(self, session, old, keep):
        prev = self._summary[session]
        new_summary = summarize(prev, old, self._llm)   # LLM + fallback bên trong
        with self._lock:
            self._summary[session] = new_summary
            # bỏ phần đã tóm tắt khỏi recent, giữ `keep` tin cuối
            recent = self._recent[session]
            while len(recent) > keep:
                recent.popleft()