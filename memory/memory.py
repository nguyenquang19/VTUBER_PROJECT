"""
memory.py — [BƯỚC E] Bộ nhớ dài hạn + reflection (biết lớn lên).

CHƯA HOÀN THIỆN — stub. Sẽ code đầy đủ ở Bước E.

Hai phần:
  1) MemoryStore: ghi/truy hồi ký ức (vector DB Qdrant + embedding bge-m3).
     - ADD/UPDATE/DELETE (không nhồi rác), temporal boosting, forgetting.
     - Khoá theo user_id để nhớ từng khán giả.
  2) Reflection: sau mỗi buổi -> LLM tóm tắt transcript -> rút lesson
     -> cập nhật memory + persona_evolving.yaml + nạp topic cho monologue.

Kết nối quan trọng: reflection -> monologue.add_topic(...) để buổi sau
AI có chuyện mới/nhớ người quen mà tự nói (đây là "lớn lên").

RANH GIỚI: reflection KHÔNG được sửa persona_core.yaml (chống drift).
"""
from __future__ import annotations


class MemoryStore:
    def __init__(self, collection: str = "vtuber_memory") -> None:
        self.collection = collection
        # TODO(BƯỚC E): kết nối Qdrant + load embedding bge-m3

    async def add(self, user_id: str, text: str, meta: dict | None = None) -> None:
        raise NotImplementedError("Bước E")

    async def query(self, text: str, user_id: str | None = None, k: int = 5) -> list[str]:
        raise NotImplementedError("Bước E")


class Reflection:
    def __init__(self, llm, memory: MemoryStore) -> None:
        self.llm = llm
        self.memory = memory

    async def run(self, transcript: list[dict]) -> dict:
        # TODO(BƯỚC E): tóm tắt + rút lesson + cập nhật memory/persona_evolving
        raise NotImplementedError("Bước E")
