"""AppraisalTable — Tầng 2 tra bảng category → target dict (Phase 7.5.B).

Đọc từ config/emotion_appraisal.yaml. Không có logic ngoài lookup — mọi cân
chỉnh số ở YAML để không phải sửa code khi tune.
"""
from __future__ import annotations

from typing import Any


class AppraisalTable:
    def __init__(
        self,
        mapping: dict[str, dict[str, float]],
        tone_flags: dict[str, str] | None = None,
        cause_intents: dict[str, str] | None = None,
    ) -> None:
        self._mapping: dict[str, dict[str, float]] = {
            k: {d: float(v) for d, v in (targets or {}).items()}
            for k, targets in mapping.items()
        }
        self._tone_flags: dict[str, str] = dict(tone_flags or {})
        self._cause_intents: dict[str, str] = dict(cause_intents or {})

    @classmethod
    def from_loader(cls, loader) -> "AppraisalTable":
        mapping = loader.get("emotion_appraisal", "appraisal", {}) or {}
        tone = loader.get("emotion_appraisal", "tone_flags", {}) or {}
        causes = loader.get("emotion_appraisal", "cause_intents", {}) or {}
        return cls(mapping=mapping, tone_flags=tone, cause_intents=causes)

    def cause_intent(self, category: str) -> str | None:
        """A4: cụm intent CANONICAL cho category (VD 'buông lời khịa tớ'). None nếu
        category không đáng gắn cause (neutral/question/timer)."""
        return self._cause_intents.get(category)

    def target_for(self, category: str) -> dict[str, float]:
        """Trả dict target 0-10 cho từng chiều. `{}` nếu category không đổi target
        (VD chat_question_normal, chat_neutral)."""
        return dict(self._mapping.get(category, {}))

    def tone_flag(self, category: str) -> str | None:
        """Trả tên cờ tone (VD 'force_gentle_tone') hoặc None."""
        return self._tone_flags.get(category)

    def known_categories(self) -> list[str]:
        return sorted(self._mapping.keys())
