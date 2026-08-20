"""MemoryExtractor — biến turn hội thoại thành MemoryEntry (Phase 7.F).

Nguồn dữ liệu: sau khi LLMTurnRunner hoàn tất 1 turn (user input + Mai output
+ parsed mood + viewer/session context), extractor quyết định:
  - Có nên persist không (skip câu nhỏ / greeting)
  - Tier gì (SESSION cho câu thường, PERSISTENT cho preference/fact về viewer)
  - Importance dựa heuristic (mood intensity + độ dài)
  - Tags từ mood + trigger type

The extractor intentionally uses regex and length heuristics instead of another
LLM call so background persistence stays deterministic and bounded.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from interfaces.memory import MemoryEntry, MemoryTier
from services.memory.config import MemoryRuntimeConfig

# Regex bắt "preference statement" từ user — signal PERSISTENT tier.
# Ví dụ: "tôi thích cà phê", "mai nhớ nhé, tôi tên An", "tớ ghét mưa"
_PREFERENCE_PATTERNS = [
    r"\btớ\s+(?:thích|ghét|yêu|sợ|không\s+thích)\b",
    r"\btôi\s+(?:thích|ghét|yêu|sợ|không\s+thích)\b",
    r"\bmình\s+(?:thích|ghét|yêu|sợ|không\s+thích)\b",
    r"\bmai\s+nhớ\b",
    r"\btôi\s+tên\b",
    r"\btớ\s+tên\b",
    r"\bmình\s+tên\b",
    r"\bsinh\s+nhật\s+(?:của\s+)?(?:tôi|tớ|mình)\b",
]
_PREFERENCE_RE = re.compile("|".join(_PREFERENCE_PATTERNS), re.IGNORECASE)

# Câu ngắn hơn ngưỡng này bỏ qua (greeting, ack ngắn)
_MIN_CONTENT_CHARS = 15


@dataclass(frozen=True)
class TurnData:
    """Payload từ LLMTurnRunner — extractor không phụ thuộc runner class."""

    user_input: str
    mai_output: str          # text đã strip mood block
    mood_dominant: str | None = None
    mood_intensity: int | None = None    # 0-10, cao nhất trong 5 mood
    viewer_id: str | None = None
    session_id: str | None = None
    trigger_type: str | None = None
    timestamp: datetime | None = None
    delivery_verified: bool = False
    outcome_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("user_input", "mai_output"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"memory turn {name} must be a string")
        for name in ("mood_dominant", "viewer_id", "session_id", "trigger_type", "outcome_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"memory turn {name} must be a non-empty string")
        if self.mood_intensity is not None and (
            isinstance(self.mood_intensity, bool)
            or not isinstance(self.mood_intensity, int)
            or not 0 <= self.mood_intensity <= 10
        ):
            raise ValueError("memory turn mood_intensity must be an integer from 0 to 10")
        if not isinstance(self.delivery_verified, bool):
            raise ValueError("memory turn delivery_verified must be boolean")
        if self.timestamp is not None:
            _utc(self.timestamp)


class MemoryExtractor:
    """Stateless — an toàn tạo 1 instance global."""

    def __init__(
        self,
        min_chars: int = _MIN_CONTENT_CHARS,
        promote_intensity: int = 7,
    ) -> None:
        """`promote_intensity`: mood ≥ 7 → tag high_intensity + importance boost."""
        if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars <= 0:
            raise ValueError("memory extractor min_chars must be a positive integer")
        if (
            isinstance(promote_intensity, bool)
            or not isinstance(promote_intensity, int)
            or not 0 <= promote_intensity <= 10
        ):
            raise ValueError("memory extractor promote_intensity must be from 0 to 10")
        self.min_chars = min_chars
        self.promote_intensity = promote_intensity

    @classmethod
    def from_loader(cls, loader: Any) -> "MemoryExtractor":
        config = MemoryRuntimeConfig.from_loader(loader)
        return cls(
            min_chars=config.extractor_min_chars,
            promote_intensity=config.extractor_promote_intensity,
        )

    def extract(self, turn: TurnData) -> MemoryEntry | None:
        """Trả 1 MemoryEntry để write, hoặc None nếu turn không đáng lưu."""
        if not isinstance(turn, TurnData):
            raise ValueError("memory extractor requires TurnData")
        if turn.delivery_verified is not True:
            return None
        if not isinstance(turn.outcome_id, str) or not turn.outcome_id.strip():
            return None
        # 1. Skip câu quá ngắn (greeting only)
        if len(turn.user_input.strip()) < self.min_chars and len(turn.mai_output.strip()) < self.min_chars:
            return None

        # 2. Xác định tier: user statement preference → PERSISTENT, còn lại SESSION
        is_preference = bool(_PREFERENCE_RE.search(turn.user_input))
        tier = MemoryTier.PERSISTENT if is_preference else MemoryTier.SESSION

        # 3. Content: gộp cả user input + Mai output (cả 2 quan trọng cho callback)
        content = self._compose_content(turn)

        # 4. Importance: base 0.5, boost nếu preference hoặc mood mạnh
        importance = 0.5
        if is_preference:
            importance = 0.85
        elif turn.mood_intensity is not None and turn.mood_intensity >= self.promote_intensity:
            importance = 0.7

        # 5. Tags: gom mood + trigger + tier signals
        tags: list[str] = []
        if turn.mood_dominant:
            tags.append(f"mood:{turn.mood_dominant}")
        if turn.trigger_type:
            tags.append(f"trigger:{turn.trigger_type}")
        if is_preference:
            tags.append("preference")
        if turn.mood_intensity is not None and turn.mood_intensity >= self.promote_intensity:
            tags.append("high_intensity")

        # 6. Metadata: viewer/session để filter query + debug
        metadata: dict[str, Any] = {}
        if turn.viewer_id:
            metadata["viewer_id"] = turn.viewer_id
        if turn.session_id:
            metadata["session_id"] = turn.session_id
        if turn.mood_dominant:
            metadata["mood_dominant"] = turn.mood_dominant
        if turn.mood_intensity is not None:
            metadata["mood_intensity"] = turn.mood_intensity
        metadata["provenance"] = "verified_delivery"
        metadata["verified"] = True
        metadata["outcome_id"] = turn.outcome_id.strip()

        return MemoryEntry(
            entry_id=uuid.uuid4().hex,
            content=content,
            timestamp=_utc(turn.timestamp or datetime.now(timezone.utc)),
            tier=tier,
            importance=importance,
            tags=tuple(tags),
            metadata=metadata,
        )

    @staticmethod
    def _compose_content(turn: TurnData) -> str:
        """Format 'User: X | Mai: Y' — embedding sẽ match cả 2 phía khi query."""
        u = turn.user_input.strip()
        m = turn.mai_output.strip()
        if not u:
            return f"Mai: {m}"
        if not m:
            return f"User: {u}"
        return f"User: {u} | Mai: {m}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory turn timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
