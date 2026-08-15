"""PromptCache — quản lý persona prefix cố định (ARCHITECTURE 8.2, 1.C).

Spec gốc 8.2/10.3 nói cache persona qua file `--prompt-cache`. NHƯNG flag đó là
của llama-cli, KHÔNG phải llama-server (xem process_manager 1.A + docs/06_OPERATIONS_AND_TROUBLESHOOTING.md). Caching
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


_REPO_ROOT = Path(__file__).resolve().parents[2]


class PromptCacheError(Exception):
    pass


def _resolve_prompt_path(value: str | Path) -> Path:
    """Resolve production prompt paths independently from the process cwd."""
    path = Path(value)
    return path if path.is_absolute() else _REPO_ROOT / path


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
        path = _resolve_prompt_path(loader.get(
            "models", "llm_main.persona_prompt_path", "config/prompts/persona_system.txt"
        ))
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if not text.strip():
            raise PromptCacheError(f"không thấy persona prompt: {path}")
        # 1.3.0: nối lore (chi tiết nhân vật) vào cùng prefix. Cả hai đều tĩnh nên
        # prefix vẫn byte-ổn định → KV cache reuse. Thiếu file lore → chỉ dùng persona.
        lore_path = loader.get("models", "llm_main.lore_prompt_path", None)
        resolved_lore = _resolve_prompt_path(lore_path) if lore_path else None
        if resolved_lore is not None and resolved_lore.is_file():
            lore = resolved_lore.read_text(encoding="utf-8").strip()
            if lore:
                text = f"{text.strip()}\n\n{lore}"
        return cls(text)

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
