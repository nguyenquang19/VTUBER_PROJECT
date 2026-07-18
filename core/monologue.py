"""
monologue.py — Monologue Engine (Tầng 2): trái tim của "tự nói / làm chủ sân khấu".

Đây là thứ làm AI trông như streamer thật chứ không phải bot chờ chat.
Nó giữ một HÀNG ĐỢI TOPIC. Orchestrator hỏi "giờ tự nói gì?" -> engine đưa ra
một topic (hoặc câu tiếp theo phát triển topic đang dở).

V1 (bản khung):
- Nguồn topic: persona (sở thích) + memory (mock) + hạt giống tĩnh dự phòng.
- Bắt buộc PHÁT TRIỂN mỗi topic >= min_develop câu trước khi đổi (chống nhảy loạn).
- Chống lặp bằng cooldown.
Khi ghép model thật: chỉ cần nạp thêm topic từ vector DB / lesson cards / sự kiện.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import random
import time


@dataclass
class Topic:
    text: str                 # mô tả topic để đưa vào prompt LLM
    source: str               # "persona" | "memory" | "event" | "seed"
    priority: int = 0
    used_at: float = 0.0      # lần cuối dùng (cho cooldown)
    develop_count: int = 0    # đã phát triển bao nhiêu câu trong lượt hiện tại


class MonologueEngine:
    def __init__(
        self,
        persona_interests: list[str] | None = None,
        seed_topics: list[str] | None = None,
        min_develop: int = 2,       # tối thiểu 2 câu/topic trước khi đổi
        max_develop: int = 4,       # tối đa, để không lê thê một chủ đề
        cooldown_sec: float = 180,  # 3 phút mới cho lặp lại 1 topic
    ) -> None:
        self.min_develop = min_develop
        self.max_develop = max_develop
        self.cooldown_sec = cooldown_sec

        self._queue: list[Topic] = []
        self._current: Topic | None = None
        self._recent_texts: list[str] = []  # chống lặp nội dung gần đây

        # Nạp nguồn ban đầu
        for it in (persona_interests or []):
            self._queue.append(Topic(text=f"Nói về sở thích: {it}", source="persona", priority=2))
        for s in (seed_topics or _DEFAULT_SEEDS):
            self._queue.append(Topic(text=s, source="seed", priority=0))
        random.shuffle(self._queue)

    # ---- API cho orchestrator ----

    def has_material(self) -> bool:
        return self._current is not None or bool(self._queue)

    def next_beat(self) -> dict:
        """
        Trả về "nhịp nói tiếp theo" cho monologue:
          - Nếu đang giữa một topic chưa phát triển đủ -> tiếp tục topic đó.
          - Nếu đã đủ (hoặc chưa có topic) -> rút topic mới liên quan.
        Trả về dict context để orchestrator đưa vào SpeakRequest.
        """
        self._ensure_queue()

        # Quyết định tiếp tục hay đổi topic
        if self._current is None:
            self._current = self._pick_new_topic()
            self._current.develop_count = 0
            phase = "start"
        elif self._current.develop_count >= self.max_develop:
            # đã phát triển đủ nhiều -> chuyển topic mới liên quan
            self._retire_current()
            self._current = self._pick_new_topic()
            self._current.develop_count = 0
            phase = "transition"
        elif self._current.develop_count >= self.min_develop and random.random() < 0.4:
            # đã đủ tối thiểu -> có xác suất chuyển sang topic khác cho đỡ nhàm
            self._retire_current()
            self._current = self._pick_new_topic()
            self._current.develop_count = 0
            phase = "transition"
        else:
            phase = "develop"

        self._current.develop_count += 1
        self._current.used_at = time.time()

        return {
            "kind": "monologue",
            "topic": self._current.text,
            "phase": phase,            # start | develop | transition
            "develop_count": self._current.develop_count,
        }

    def register_spoken(self, text: str) -> None:
        """Ghi lại câu AI vừa nói (để chống lặp nội dung)."""
        self._recent_texts.append(text)
        if len(self._recent_texts) > 20:
            self._recent_texts.pop(0)

    def add_topic(self, text: str, source: str = "memory", priority: int = 1) -> None:
        """Nạp topic mới (từ memory/lesson/sự kiện). Gọi từ ngoài khi có nguyên liệu."""
        self._queue.append(Topic(text=text, source=source, priority=priority))

    def interrupt_for_chat(self) -> None:
        """
        Khi bị chat chen vào: KHÔNG vứt topic đang dở.
        Giữ nguyên _current để sau khi trả lời chat xong còn 'về mạch'.
        """
        # cố ý không làm gì với _current -> giữ mạch
        pass

    # ---- nội bộ ----

    def _ensure_queue(self) -> None:
        if not self._queue and self._current is None:
            # cạn ý -> nạp lại hạt giống dự phòng (không bao giờ để "chết")
            for s in _DEFAULT_SEEDS:
                self._queue.append(Topic(text=s, source="seed", priority=0))
            random.shuffle(self._queue)

    def _pick_new_topic(self) -> Topic:
        now = time.time()
        # ưu tiên: priority cao + không trong cooldown
        candidates = [
            t for t in self._queue
            if now - t.used_at > self.cooldown_sec
        ] or self._queue  # nếu tất cả đều cooldown, đành lấy đại

        candidates.sort(key=lambda t: (-t.priority, t.used_at))
        chosen = candidates[0]
        if chosen in self._queue:
            self._queue.remove(chosen)
        return chosen

    def _retire_current(self) -> None:
        if self._current is not None:
            # đánh dấu thời điểm dùng để cooldown có hiệu lực (chống lặp sớm)
            self._current.used_at = time.time()
            self._current.develop_count = 0
            # đưa topic đã dùng về cuối queue (cho lặp lại SAU cooldown)
            self._queue.append(self._current)
            self._current = None


# Kho "hạt giống" tĩnh — dùng khi cạn ý, đảm bảo không bao giờ "chết sân khấu".
# Đây chỉ là placeholder trung tính; nội dung thật sẽ do persona quyết định.
_DEFAULT_SEEDS = [
    "Hỏi chat hôm nay mọi người thế nào",
    "Kể một chuyện vu vơ vừa nghĩ ra",
    "Than thở nhẹ về việc làm streamer AI",
    "Đặt một câu hỏi ngớ ngẩn dễ thương cho chat",
    "Nhận xét về không khí stream lúc này",
    "Tự troll bản thân một câu",
]
