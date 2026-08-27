"""Build privacy-safe meaning entries from verified conversation outcomes.

Nguồn dữ liệu: sau khi LLMTurnRunner hoàn tất 1 turn (user input + Mai output
+ parsed mood + viewer/session context), extractor quyết định:
  - Có nên persist không (skip câu nhỏ / greeting)
  - Tier gì (SESSION cho meaning chung, PERSISTENT cho preference an toàn)
  - Importance dựa heuristic (mood intensity + độ dài)
  - Tags từ mood + trigger type

The extractor intentionally uses regex and length heuristics instead of another
LLM call so background persistence stays deterministic and bounded. It never
stores the source transcript or Mai's generated response.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from interfaces.memory import MemoryEntry, MemoryTier
from services.data.sanitize import mask_pii
from services.memory.config import MemoryRuntimeConfig

# Only a bounded preference meaning is retained. Direct identity facts are
# rejected instead of trying to turn them into long-lived semantic memory.
_PREFERENCE_RE = re.compile(
    r"\b(?:tớ|tôi|mình)\s+"
    r"(?P<verb>không\s+thích|thích|ghét|yêu|sợ)\s+"
    r"(?P<object>[^\n]{1,512})",
    re.IGNORECASE,
)
_SENSITIVE_IDENTITY_RE = re.compile(
    r"\b(?:tớ|tôi|mình)\s+tên\b|"
    r"\btên\s+(?:của\s+)?(?:tớ|tôi|mình)\b|"
    r"\bsinh\s+nhật\s+(?:của\s+)?(?:tớ|tôi|mình)\b|"
    r"\b(?:địa\s+chỉ|số\s+điện\s+thoại|email|cccd|cmnd|hộ\s+chiếu)\b",
    re.IGNORECASE,
)

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
        content_max_chars: int = 4000,
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
        if (
            isinstance(content_max_chars, bool)
            or not isinstance(content_max_chars, int)
            or content_max_chars <= 0
        ):
            raise ValueError("memory extractor content_max_chars must be positive")
        self.content_max_chars = content_max_chars

    @classmethod
    def from_loader(cls, loader: Any) -> "MemoryExtractor":
        config = MemoryRuntimeConfig.from_loader(loader)
        return cls(
            min_chars=config.extractor_min_chars,
            promote_intensity=config.extractor_promote_intensity,
            content_max_chars=config.content_max_chars,
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

        # Direct identity facts are not memory material. Sanitizing them into a
        # placeholder would retain no useful meaning and invites later recitation.
        if _SENSITIVE_IDENTITY_RE.search(turn.user_input):
            return None

        # 2. Xác định tier: safe preference meaning → PERSISTENT, còn lại SESSION
        preference = _PREFERENCE_RE.search(turn.user_input)
        is_preference = preference is not None
        tier = MemoryTier.PERSISTENT if is_preference else MemoryTier.SESSION

        # 3. Content is a compact meaning projection, never the source transcript.
        content = self._compose_meaning(turn, preference)
        if not content:
            return None

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
            metadata["viewer_id"] = turn.viewer_id.strip()
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

    def _compose_meaning(
        self,
        turn: TurnData,
        preference: re.Match[str] | None,
    ) -> str:
        if preference is not None:
            verb = " ".join(preference.group("verb").lower().split())
            subject = " ".join((mask_pii(preference.group("object")) or "").split())
            if not subject:
                return ""
            meaning = f"viewer_preference: {verb} {subject}"
        else:
            parts = ["verified_conversation"]
            if turn.trigger_type:
                parts.append(f"trigger:{turn.trigger_type}")
            if turn.mood_dominant:
                parts.append(f"mood:{turn.mood_dominant}")
            meaning = "; ".join(parts)
        return meaning[: self.content_max_chars]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory turn timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
