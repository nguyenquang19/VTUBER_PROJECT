"""Canned-response terminal level for the LLM fallback chain.

Khi Gemma 12B fail (timeout/crash), Mai vẫn phải "nói được gì đó" để không đứng
hình. Chọn 1 câu canned theo dominant mood gần nhất (nếu có), không thì "default".

Config-over-code (N6): template lấy từ config models.yaml `llm_canned.responses`.
"""
from __future__ import annotations

import random

from interfaces.animation import MoodState
from services.llm.parser import ParsedResponse

_FALLBACK_POOL = ["..."]


class CannedResponder:
    def __init__(self, responses: dict[str, list[str]], rng: random.Random | None = None) -> None:
        self._responses = responses or {}
        self._rng = rng or random.Random()
        self._last_mood: MoodState | None = None

    @classmethod
    def from_loader(cls, loader, rng: random.Random | None = None) -> "CannedResponder":
        responses = loader.get("models", "llm_canned.responses", {}) or {}
        return cls(responses, rng=rng)

    def update_mood(self, mood: MoodState | None) -> None:
        """Cập nhật mood gần nhất (từ turn thành công) để canned chọn cho hợp."""
        self._last_mood = mood

    def pick(self) -> str:
        key = self._last_mood.dominant() if self._last_mood is not None else "default"
        pool = self._responses.get(key) or self._responses.get("default") or _FALLBACK_POOL
        return self._rng.choice(pool)

    def build(self) -> ParsedResponse:
        """ParsedResponse canned: mood giữ mood gần nhất, ok=False (không phải model)."""
        return ParsedResponse(
            text=self.pick(),
            mood=self._last_mood or MoodState(),
            ok=False,
            raw="<canned>",
        )
