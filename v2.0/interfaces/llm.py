"""Streaming language-model service contract.

The production implementation is `services.llm.llama_cpp_llm.LlamaCppLLMService`
backed by llama.cpp.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping

from pydantic import BaseModel, Field

from interfaces.base import Service


class ChatMessage(BaseModel):
    """1 lượt hội thoại cho /v1/chat/completions."""

    role: str  # "system" | "user" | "assistant"
    content: str


class LLMWorkloadClass(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"


class LLMContextOverflowPolicy(str, Enum):
    COMPACT = "compact"
    REJECT = "reject"


@dataclass(frozen=True)
class LLMJsonSchemaResponse:
    """Strict llama.cpp JSON Schema response declaration."""

    name: str
    schema: Mapping[str, Any]
    strict: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name != self.name.strip() or not self.name:
            raise ValueError("JSON schema response name must be trimmed and non-empty")
        if len(self.name) > 64:
            raise ValueError("JSON schema response name exceeds 64 characters")
        if self.strict is not True:
            raise ValueError("JSON schema response must remain strict")
        if not isinstance(self.schema, Mapping):
            raise ValueError("JSON schema response schema must be a mapping")
        object.__setattr__(self, "schema", _freeze_json(self.schema))

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": self.name,
                "strict": True,
                "schema": _plain_json(self.schema),
            },
        }


class LLMRequest(BaseModel):
    request_id: str
    # `prompt` = tiện cho caller đơn giản (1 lượt user). `messages` = đầy đủ
    # (persona system + history) do prompt_manager (1.C) dựng. Nếu có messages
    # thì ưu tiên; nếu không thì bọc prompt thành 1 user message.
    prompt: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int = 300
    temperature: float = 0.85
    seed: int | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    workload_class: LLMWorkloadClass = LLMWorkloadClass.LIVE
    context_overflow_policy: LLMContextOverflowPolicy = LLMContextOverflowPolicy.COMPACT
    response_format: LLMJsonSchemaResponse | None = None

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


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("JSON schema contains an unsupported value")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value
