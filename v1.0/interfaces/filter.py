"""Filter interface (ARCHITECTURE 7.5).

N7: filter fail-open — lỗi thì cho qua + log, không block. Implementation
`RuleFilter` ở Phase 3 (spec 8.3A).
"""
from __future__ import annotations

from abc import abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from interfaces.base import Service


class FilterCategory(str, Enum):
    PERSONA_BREAK = "persona_break"
    MANIPULATION = "manipulation"
    EXPLICIT = "explicit"
    HARMFUL = "harmful"


class FilterVerdict(BaseModel):
    passed: bool
    categories_hit: list[FilterCategory] = Field(default_factory=list)
    severity: str = "low"           # low / medium / high
    suggested_action: str = "allow"  # allow / regenerate / replace / block
    reason: str = ""
    latency_ms: int = 0

    @classmethod
    def allow(cls, latency_ms: int = 0, reason: str = "") -> FilterVerdict:
        return cls(passed=True, latency_ms=latency_ms, reason=reason)

    @classmethod
    def fail_open(cls, reason: str, latency_ms: int = 0) -> FilterVerdict:
        """N7: filter lỗi → cho qua nhưng ghi rõ lý do để log/dashboard thấy."""
        return cls(
            passed=True,
            severity="low",
            suggested_action="allow",
            reason=f"fail-open: {reason}",
            latency_ms=latency_ms,
        )


class FilterService(Service):
    @abstractmethod
    async def check(self, text: str, context: dict[str, Any]) -> FilterVerdict:
        """Kiểm tra text. KHÔNG raise — lỗi nội bộ phải trả fail_open verdict."""
