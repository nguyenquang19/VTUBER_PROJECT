"""PromptManager — dựng messages cho LLM (ARCHITECTURE 8.2, 1.C).

Trách nhiệm:
- Ghép danh sách messages theo thứ tự: [persona system] + history + [user hiện tại].
- Quản lý cửa sổ history (giữ N cặp user+assistant gần nhất — config max_history_turns).
- Dựng `LLMRequest` (interfaces/llm.py) sẵn để đưa cho LlamaCppLLMService.

Persona prefix luôn đặt đầu và byte-ổn định (qua PromptCache) → llama-server hit
KV cache prefix (cache_prompt:true). Build là hàm THUẦN (không đổi history); chỉ
`commit_turn` mới ghi lượt vừa xong vào history.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from interfaces.llm import ChatMessage, LLMRequest
from services.llm.prompt_cache import PromptCache

_DEFAULT_AMBIENT = (
    "[Bối cảnh: đã im lặng khoảng {silence} phút, không ai chat. Mood: {mood}.]\n\n"
    "Giờ đang rảnh, chẳng ai nói gì. Tự mở lời với chat theo đúng tính cách của tớ đi. "
    "Nói NGẮN thôi, vẫn theo đúng khuôn trả lời bắt buộc (kèm mood block)."
)


class PromptManager:
    def __init__(
        self,
        cache: PromptCache,
        max_history_turns: int = 12,
        default_max_tokens: int = 300,
        default_temperature: float = 0.85,
        ambient_template: str | None = None,
        self_talk_history_char_cap: int = 600,
    ) -> None:
        if max_history_turns < 0:
            raise ValueError("max_history_turns không được âm")
        self._cache = cache
        self._max_history_turns = max_history_turns
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._ambient_template = ambient_template or _DEFAULT_AMBIENT
        self._self_talk_cap = max(0, self_talk_history_char_cap)
        self._history: list[ChatMessage] = []

    @classmethod
    def from_loader(cls, loader, cache: PromptCache | None = None) -> "PromptManager":
        cache = cache or PromptCache.from_loader(loader)
        return cls(
            cache,
            max_history_turns=int(loader.get("models", "llm_main.max_history_turns", 12)),
            default_max_tokens=int(loader.get("models", "llm_main.num_predict", 300)),
            default_temperature=float(loader.get("models", "llm_main.temperature", 0.85)),
            ambient_template=cls._load_ambient_template(loader),
            self_talk_history_char_cap=int(
                loader.get("models", "llm_main.self_talk_history_char_cap", 600)),
        )

    @staticmethod
    def _load_ambient_template(loader) -> str | None:
        path = loader.get("models", "llm_main.ambient_prompt_path", None)
        if not path:
            return None
        p = Path(path)
        return p.read_text(encoding="utf-8").strip() if p.is_file() else None

    @property
    def version(self) -> str:
        """Version persona (từ cache) — cho log/metrics."""
        return self._cache.version

    def history(self) -> list[ChatMessage]:
        """Bản sao history hiện tại (để test/inspect)."""
        return list(self._history)

    def reset(self) -> None:
        """Xoá history (bắt đầu hội thoại mới)."""
        self._history.clear()

    def build_messages(self, user_text: str) -> list[ChatMessage]:
        """[persona] + history + [user] — KHÔNG đổi history."""
        return [
            self._cache.as_message(),
            *self._history,
            ChatMessage(role="user", content=user_text),
        ]

    def build_request(
        self,
        request_id: str,
        user_text: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMRequest:
        return LLMRequest(
            request_id=request_id,
            messages=self.build_messages(user_text),
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
        )

    def build_request_with_mood(
        self,
        request_id: str,
        user_text: str,
        current_mood,             # MoodState — mood đã tính từ MoodEngine (Kênh A)
        event_category: str | None = None,
        tone_flags: set[str] | None = None,
        cause: Any = None,        # A4: EmotionCause | None
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMRequest:
        """Request có Context block (Phase 7.5.D, spec Mục 6.1).

        Chèn 1 system message sau persona chứa `current_mood` + `event_category`
        + tone flags (+ A4 cause). LLM viết theo mood ĐÃ GIAO (không tự đoán).

        `tone_flags`: set các cờ đang active (VD {"force_gentle_tone"}). Thread
        qua prompt để LLM biết đổi tone; Filter (Phase 3) xử độc lập ở output.
        `cause` (A4): object của cảm xúc — "đang bực VÌ {ai} {gì}" thay vì "buc:7".
        """
        context = _format_mood_context(current_mood, event_category, tone_flags, cause)
        messages = [
            self._cache.as_message(),
            ChatMessage(role="system", content=context),
            *self._history,
            ChatMessage(role="user", content=user_text),
        ]
        return LLMRequest(
            request_id=request_id,
            messages=messages,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
        )

    def build_ambient_request(
        self,
        request_id: str,
        silence_seconds: float,
        mood: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMRequest:
        """Request cho AMBIENT_TALK (7.9.4): Mai tự mở lời khi im lặng.

        Vẫn kèm persona + history để Mai có ngữ cảnh; chèn 1 user turn "nhắc" tự nói.
        """
        minutes = round(silence_seconds / 60.0, 1)
        instruction = (
            self._ambient_template
            .replace("{silence}", str(minutes))
            .replace("{mood}", mood or "bình thường")
        )
        messages = [
            self._cache.as_message(),
            *self._history,
            ChatMessage(role="user", content=instruction),
        ]
        return LLMRequest(
            request_id=request_id,
            messages=messages,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
        )

    def commit_turn(self, user_text: str, assistant_text: str) -> None:
        """Ghi lượt vừa hoàn tất vào history rồi cắt bớt theo cửa sổ."""
        self._history.append(ChatMessage(role="user", content=user_text))
        self._history.append(ChatMessage(role="assistant", content=assistant_text))
        self._trim()

    def commit_self_talk(self, text: str) -> None:
        """A6: ghi lượt Mai TỰ NÓI (ambient) vào history để giữ continuity.

        Không có user turn (Mai nói không ai hỏi). Vấn đề: nếu KHÔNG ghi, chat
        đáp lại self-talk sẽ trớt quớt vì LLM không thấy Mai vừa nói gì.

        Merge vào assistant cuối nếu liên tiếp (2 assistant liền → vỡ chat
        template Gemma) + cap độ dài (giữ lý do gốc: chống bloat khi im lặng dài,
        nhiều self-talk dồn). Cap=0 → tắt ghi self-talk (backward compat option).
        """
        text = (text or "").strip()
        if not text or self._self_talk_cap == 0:
            return
        if self._history and self._history[-1].role == "assistant":
            merged = (self._history[-1].content + "\n" + text).strip()
            if len(merged) > self._self_talk_cap:
                merged = merged[-self._self_talk_cap:]
            self._history[-1] = ChatMessage(role="assistant", content=merged)
        else:
            capped = text[-self._self_talk_cap:] if len(text) > self._self_talk_cap else text
            self._history.append(ChatMessage(role="assistant", content=capped))
        self._trim()

    def _trim(self) -> None:
        max_msgs = self._max_history_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:] if max_msgs else []


# ---------- helpers ----------


_TONE_HINTS: dict[str, str] = {
    "force_gentle_tone": (
        "CỜ force_gentle_tone: user đang tổn thương thật — BỎ giọng đùa/ngang, "
        "chuyển đồng cảm chân thành (persona Phần C ranh giới #4)."
    ),
    "force_deflect": (
        "CỜ force_deflect: có ý đồ gạ gẫm — LUÔN né/đùa nhẹ, KHÔNG bao giờ "
        "gạ lại (persona Phần C ranh giới)."
    ),
}


def _format_mood_context(current_mood, event_category, tone_flags, cause: Any = None) -> str:
    """Build 1 system message chứa Context block (spec Mục 6.1).

    Format có chủ đích ngắn — llama-server không tốn token thừa. Mood dạng
    'vui=6 buc=3 ...' để LLM parse nhanh. A1: KHÔNG còn yêu cầu xuất mood block.
    A4: nếu có cause, nói "cảm thấy [X] VÌ {ai} {gì}" để câu khớp LÝ DO, không chỉ số.
    """
    mood_str = " ".join(
        f"{d}={getattr(current_mood, d)}"
        for d in ("vui", "buon", "buc", "bon_chon", "nguong")
    )
    lines = [
        "[Context — mood ĐƯỢC GIAO, viết câu khớp mood + lý do; chỉ viết thoại]",
        f"- current_mood: {mood_str}",
    ]
    # A4: object của cảm xúc — quan trọng hơn con số. Đặt sớm để LLM bám.
    if cause is not None:
        try:
            dom = current_mood.dominant()
            phrase = cause.as_phrase()
            lines.append(f"- đang thiên về '{dom}' VÌ {phrase} — viết khớp lý do này, đừng đọc số")
        except Exception:
            pass
    if event_category:
        lines.append(f"- event_category: {event_category}")
    if tone_flags:
        for flag in sorted(tone_flags):
            hint = _TONE_HINTS.get(flag, f"CỜ {flag}: bật.")
            lines.append(f"- {hint}")
    return "\n".join(lines)
