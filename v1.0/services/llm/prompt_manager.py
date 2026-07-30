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

from interfaces.llm import ChatMessage, LLMRequest
from services.llm.prompt_cache import PromptCache


class PromptManager:
    def __init__(
        self,
        cache: PromptCache,
        max_history_turns: int = 12,
        default_max_tokens: int = 300,
        default_temperature: float = 0.85,
    ) -> None:
        if max_history_turns < 0:
            raise ValueError("max_history_turns không được âm")
        self._cache = cache
        self._max_history_turns = max_history_turns
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._history: list[ChatMessage] = []

    @classmethod
    def from_loader(cls, loader, cache: PromptCache | None = None) -> "PromptManager":
        cache = cache or PromptCache.from_loader(loader)
        return cls(
            cache,
            max_history_turns=int(loader.get("models", "llm_main.max_history_turns", 12)),
            default_max_tokens=int(loader.get("models", "llm_main.num_predict", 300)),
            default_temperature=float(loader.get("models", "llm_main.temperature", 0.85)),
        )

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

    def commit_turn(self, user_text: str, assistant_text: str) -> None:
        """Ghi lượt vừa hoàn tất vào history rồi cắt bớt theo cửa sổ."""
        self._history.append(ChatMessage(role="user", content=user_text))
        self._history.append(ChatMessage(role="assistant", content=assistant_text))
        self._trim()

    def _trim(self) -> None:
        max_msgs = self._max_history_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:] if max_msgs else []
