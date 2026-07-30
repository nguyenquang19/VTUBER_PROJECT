"""PromptCache — quản lý persona prefix cố định (ARCHITECTURE 8.2, 1.C).

Spec gốc 8.2/10.3 nói cache persona qua file `--prompt-cache`. NHƯNG flag đó là
của llama-cli, KHÔNG phải llama-server (xem process_manager 1.A + STATE.md). Caching
thật của server hoạt động qua `cache_prompt: true` trong request: server tự giữ KV
cache cho phần PREFIX giống hệt giữa các turn.

=> Vai trò PromptCache ở đây: **giữ persona system message byte-ổn định** để prefix
luôn trùng giữa các turn → server hit KV cache → giảm TTFT. Kèm version hash để biết
khi nào persona đổi (đổi = cache prefix invalidate, log lại).

Không phải Service (không I/O runtime) → class thường, không qua interface.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from interfaces.llm import ChatMessage


class PromptCacheError(Exception):
    pass


class PromptCache:
    def __init__(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise PromptCacheError("persona system prompt rỗng")
        self._text = text
        self._version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def from_file(cls, path: str | Path) -> "PromptCache":
        p = Path(path)
        if not p.is_file():
            raise PromptCacheError(f"không thấy persona prompt: {p}")
        return cls(p.read_text(encoding="utf-8"))

    @classmethod
    def from_loader(cls, loader) -> "PromptCache":
        path = loader.get(
            "models", "llm_main.persona_prompt_path", "config/prompts/persona_system.txt"
        )
        return cls.from_file(path)

    @property
    def text(self) -> str:
        return self._text

    @property
    def version(self) -> str:
        """Hash 12 ký tự của persona — đổi khi nội dung persona đổi."""
        return self._version

    def as_message(self) -> ChatMessage:
        """System message để đặt đầu danh sách messages (prefix cache)."""
        return ChatMessage(role="system", content=self._text)
