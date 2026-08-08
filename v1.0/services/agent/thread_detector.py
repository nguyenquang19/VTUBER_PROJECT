"""Rule-only detector for grounded question, promise, and story threads (M4.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.agent.types import (
    AgentEventKind, GroundedEvent, OpenThread, ThreadEvidence, ThreadKind,
    ThreadOperation, ThreadSignal,
)

_FOLLOW_UP = re.compile(
    r"\b(kể tiếp|tiếp đi|nói tiếp|rồi sao|thế còn|vậy còn|nãy|lúc nãy|continue)\b",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"\b(tớ sẽ|để tớ (?:quay lại|nói tiếp|kể tiếp|làm|kiểm tra)|lát nữa tớ|"
    r"chút nữa tớ|tớ quay lại|tớ kể tiếp|i will|i'll)\b",
    re.IGNORECASE,
)
_STORY = re.compile(
    r"\b(tớ đang kể|câu chuyện|chuyện này|hồi đó|để tớ kể|tớ kể)\b",
    re.IGNORECASE,
)
_RESOLVE = re.compile(
    r"\b(kể xong|nói xong|trả lời xong|xong chuyện|thế là hết|done with)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ThreadDetectorConfig:
    evidence_excerpt_max_chars: int = 220

    @classmethod
    def from_loader(cls, loader: Any) -> "ThreadDetectorConfig":
        value = int(
            loader.get("conversation", "detector.evidence_excerpt_max_chars", 220)
        )
        if value <= 0:
            raise ValueError("detector evidence cap must be positive")
        return cls(value)


class RuleThreadDetector:
    def __init__(self, config: ThreadDetectorConfig | None = None) -> None:
        self.config = config or ThreadDetectorConfig()

    @classmethod
    def from_loader(cls, loader: Any) -> "RuleThreadDetector":
        return cls(ThreadDetectorConfig.from_loader(loader))

    def detect(
        self, event: GroundedEvent, open_threads: tuple[OpenThread, ...],
    ) -> tuple[ThreadSignal, ...]:
        if event.kind not in (AgentEventKind.CHAT_RECEIVED, AgentEventKind.SPEECH_FINAL):
            return ()
        text = _compact(event.payload.get("text"), self.config.evidence_excerpt_max_chars)
        if not text:
            return ()
        evidence = ThreadEvidence(
            source_event_id=event.event_id,
            excerpt=text,
            detector="rule",
            confidence=event.confidence,
        )
        target = _best_target(open_threads, text)
        if target is not None and _RESOLVE.search(text):
            return (ThreadSignal(
                ThreadOperation.RESOLVE, target.kind, target.topic, target.summary,
                evidence, target_thread_id=target.thread_id, reason="explicit_completion",
            ),)
        if target is not None and _FOLLOW_UP.search(text):
            return (ThreadSignal(
                ThreadOperation.UPDATE, target.kind, target.topic, text,
                evidence, target_thread_id=target.thread_id,
            ),)
        if event.kind is AgentEventKind.SPEECH_FINAL and _PROMISE.search(text):
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.PROMISE, _topic(text), text, evidence,
            ),)
        if "?" in text:
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.QUESTION, _topic(text), text, evidence,
            ),)
        if event.kind is AgentEventKind.SPEECH_FINAL and _STORY.search(text):
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.STORY, _topic(text), text, evidence,
            ),)
        return ()


def _best_target(open_threads: tuple[OpenThread, ...], text: str) -> OpenThread | None:
    if not open_threads:
        return None
    terms = set(re.findall(r"\w+", text.casefold()))
    ranked = sorted(
        open_threads,
        key=lambda item: (
            len(terms & set(re.findall(r"\w+", f"{item.topic} {item.summary}".casefold()))),
            item.updated_at,
            item.thread_id,
        ),
        reverse=True,
    )
    return ranked[0]


def _topic(text: str) -> str:
    return " ".join(text.split())[:120]


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"
