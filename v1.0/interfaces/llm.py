"""LLM interface (ARCHITECTURE 7.4).

Interface không đổi dù backend là llama.cpp hay gì khác (P3 interface-based).
Implementation `LlamaCppLLMService` sẽ ở `services/llm/` (Phase 1, spec 8.2).
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from interfaces.base import Service


class LLMRequest(BaseModel):
    request_id: str
    prompt: str
    max_tokens: int = 300
    temperature: float = 0.85
    stop_sequences: list[str] = Field(default_factory=list)


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
