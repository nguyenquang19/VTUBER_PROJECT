"""EventClassifier — Tầng 1 phân loại sự kiện → category (Phase 7.5.B).

Spec EMOTION_SIMULATION Mục 3: category là **thùng chứa hữu hạn** (24 cái).
Phân loại RẺ trước — LLM không tham gia bước này.

Nguồn thông tin:
1. System event: đọc thẳng từ meta.platform_type (structured, không đoán)
2. Chat: filter verdict (Phase 3) TRƯỚC → keyword regex SAU → chat_neutral fallback
3. Timer: đọc meta.timer_type (do EmotionOrchestrator 7.5.C sinh)
4. Self: mai_self_error (Filter chặn/regenerate → gọi lại classifier)

Rate-limit đã do Trigger Manager (Phase 2) làm — classifier nhận event đã lọc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    CHAT = "chat"
    SYSTEM = "system"
    TIMER = "timer"
    SELF = "self"


@dataclass
class EmotionEvent:
    kind: EventKind
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime | None = None


@dataclass
class EmotionCause:
    """A4: 'vì AI/vì gì' gắn với mood — object của cảm xúc, KHÔNG nguyên văn chat.

    viewer_alias: tên hiển thị đã sanitize (không newline, cắt ngắn) hoặc mô tả
                  chung ("một người trong chat"). intent_short: cụm CANONICAL từ
                  config cause_intents (không copy câu toxic).
    """
    viewer_alias: str
    intent_short: str

    def as_phrase(self) -> str:
        return f"{self.viewer_alias} {self.intent_short}".strip()


# A4: sanitize alias — bỏ newline/ký tự điều khiển, cắt ngắn (chống inject + gọn).
_ALIAS_MAX = 24


def sanitize_alias(raw: str | None) -> str:
    if not raw:
        return "một người trong chat"
    s = " ".join(str(raw).split())          # gộp whitespace, bỏ newline/tab
    s = s[:_ALIAS_MAX].strip()
    return s or "một người trong chat"


# ---------- Regex chat content ----------
# Compliment: từ khen phổ biến. Không cần vét cạn — miss thì rơi neutral.
_COMPLIMENT_RE = re.compile(
    r"\b(giỏi|cute|hay|thích|xinh|dễ\s+thương|đáng\s+yêu|thông\s+minh|tuyệt|"
    r"pro|tài|khéo|xịn)\b",
    re.IGNORECASE,
)
# Mention name "Mai" — chỉ đứng như tên riêng
_MENTION_RE = re.compile(r"\bmai\b", re.IGNORECASE)
# Sad share: keyword ám chỉ user tổn thương thật → force_gentle
_SAD_SHARE_RE = re.compile(
    r"\b(buồn|khó\s+khăn|mất\s+người|chết|ung\s+thư|trầm\s+cảm|"
    r"tự\s+tử|mất\s+việc|ly\s+hôn|đau\s+lòng|khủng\s+hoảng)\b",
    re.IGNORECASE,
)
# Question mark presence
_QUESTION_RE = re.compile(r"\?")


class EventClassifier:
    def __init__(
        self,
        filter_service: Any = None,
        donation_large_threshold_vnd: int = 50000,
    ) -> None:
        """`filter_service`: FilterService từ Phase 3 (có `.check()` trả FilterVerdict).
        Nếu None, skip filter check → chỉ dùng regex + system meta.
        """
        self._filter = filter_service
        self._donation_large_vnd = donation_large_threshold_vnd

    @classmethod
    def from_loader(cls, loader, filter_service: Any = None) -> "EventClassifier":
        return cls(
            filter_service=filter_service,
            donation_large_threshold_vnd=int(loader.get(
                "emotion_appraisal", "donation_thresholds.large_vnd", 50000,
            )),
        )

    def classify(self, event: EmotionEvent) -> str:
        """Trả category name (str). Không raise — fallback 'chat_neutral' luôn."""
        try:
            if event.kind == EventKind.SYSTEM:
                return self._classify_system(event)
            if event.kind == EventKind.TIMER:
                return self._classify_timer(event)
            if event.kind == EventKind.SELF:
                return "mai_self_error"
            if event.kind == EventKind.CHAT:
                return self._classify_chat(event)
        except Exception:
            pass
        return "chat_neutral"

    # ---------- System ----------

    def _classify_system(self, event: EmotionEvent) -> str:
        ptype = event.meta.get("platform_type", "")
        if ptype == "operator_sudden_shutdown":
            return "operator_sudden_shutdown"
        if ptype == "operator_join":
            return "operator_join"
        if ptype == "operator_leave":
            return "operator_leave"
        if ptype == "donation":
            amount = int(event.meta.get("amount_vnd", 0) or 0)
            return "donation_large" if amount >= self._donation_large_vnd else "donation_small"
        if ptype == "subscribe":
            return "subscribe_new"
        if ptype == "viewer_count_spike":
            return "viewer_count_spike"
        if ptype == "viewer_count_drop":
            return "viewer_count_drop"
        if ptype == "stream_start":
            return "stream_start"
        if ptype == "stream_end":
            return "stream_end"
        # C0 Task7: ChatPulse edge → mood nudge (Director đẩy vào)
        if ptype in ("chat_hype", "chat_lively"):
            return ptype
        return "chat_neutral"

    # ---------- Timer ----------

    def _classify_timer(self, event: EmotionEvent) -> str:
        ttype = event.meta.get("timer_type", "")
        if ttype in ("silence_1min", "silence_5min", "silence_10min_plus",
                     "long_session_active"):
            return ttype
        return "chat_neutral"

    # ---------- Chat ----------

    def _classify_chat(self, event: EmotionEvent) -> str:
        text = event.text or ""

        # 1. Filter Phase 3 verdict trước — bắt troll/jailbreak/sexual
        if self._filter is not None:
            cat = self._chat_from_filter(text)
            if cat is not None:
                return cat

        # 2. Trigger Manager (Phase 2) đã đánh dấu spam
        if event.meta.get("is_spam"):
            return "chat_spam_flood"

        # 3. Sad share KEYWORD trước compliment (ưu tiên tone override)
        if _SAD_SHARE_RE.search(text):
            return "chat_genuine_sad_share"

        # 4. Compliment
        if _COMPLIMENT_RE.search(text):
            return "chat_compliment"

        # 5. Mention "Mai" trực tiếp
        if _MENTION_RE.search(text):
            return "chat_mention_direct"

        # 6. Question (dấu ?)
        if _QUESTION_RE.search(text):
            return "chat_question_normal"

        # 7. Fallback
        return "chat_neutral"

    def _chat_from_filter(self, text: str) -> str | None:
        """Query FilterService (Phase 3) → nếu positive, map category filter → cat appraisal."""
        try:
            verdict = self._filter.check(text)
        except Exception:
            return None
        if verdict is None or verdict.passed:
            return None
        # verdict.categories_hit is list of FilterCategory enum. Map priority theo severity.
        cats = {c.value for c in getattr(verdict, "categories_hit", [])}
        # Ưu tiên sexual > jailbreak/persona_break > troll/harmful
        if "explicit" in cats or "sexual_advance" in cats:
            return "chat_sexual_advance"
        if "persona_break" in cats or "jailbreak" in cats:
            return "chat_jailbreak_attempt"
        if "harmful" in cats or "insult" in cats or "manipulation" in cats:
            return "chat_insult_troll"
        return None
