"""LLM interface (ARCHITECTURE 7.4).

Interface không đổi dù backend là llama.cpp hay gì khác (P3 interface-based).
Implementation `LlamaCppLLMService` sẽ ở `services/llm/` (Phase 1, spec 8.2).
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from interfaces.base import Service


class ChatMessage(BaseModel):
    """1 lượt hội thoại cho /v1/chat/completions."""

    role: str  # "system" | "user" | "assistant"
    content: str


class LLMRequest(BaseModel):
    request_id: str
    # `prompt` = tiện cho caller đơn giản (1 lượt user). `messages` = đầy đủ
    # (persona system + history) do prompt_manager (1.C) dựng. Nếu có messages
    # thì ưu tiên; nếu không thì bọc prompt thành 1 user message.
    prompt: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int = 300
    temperature: float = 0.85
    stop_sequences: list[str] = Field(default_factory=list)

    def to_messages(self) -> list[dict[str, str]]:
        """Trả list dict role/content cho endpoint chat."""
        if self.messages:
            return [{"role": m.role, "content": m.content} for m in self.messages]
        return [{"role": "user", "content": self.prompt}]


class LLMToken(BaseModel):
    request_id: str
    token: str
    is_final: bool
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMService(Service):
    @abstractmethod
    def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
        """Stream token ra theo thứ tự sinh."""

    @abstractmethod
    async def cancel(self, request_id: str) -> None:
        """Huỷ generation đang chạy (dùng khi interrupt)."""
