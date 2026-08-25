"""Rule-only detector for grounded question, promise, and story threads (M4.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from interfaces.state import (
    AgentEventKind, ConversationMove, GroundedEvent, OpenThread, ThreadEvidence,
    ThreadKind, ThreadOperation, ThreadSignal, ThreadSpeaker, ThreadStatus,
)

_FOLLOW_UP = re.compile(
    r"\b(kể tiếp|tiếp đi|nói tiếp|rồi sao|thế còn|vậy còn|nãy|lúc nãy|continue)\b",
    re.IGNORECASE,
)
_EXPLICIT_FOLLOW_UP = re.compile(
    r"\b(kể tiếp|tiếp đi|nói tiếp|rồi sao|thế còn|vậy còn|continue)\b",
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
    def __init__(
        self, config: ThreadDetectorConfig | None = None, *, matcher: Any = None,
    ) -> None:
        self.config = config or ThreadDetectorConfig()
        self._matcher = matcher

    @classmethod
    def from_loader(cls, loader: Any, *, matcher: Any = None) -> "RuleThreadDetector":
        return cls(ThreadDetectorConfig.from_loader(loader), matcher=matcher)

    def detect(
        self, event: GroundedEvent, open_threads: tuple[OpenThread, ...],
    ) -> tuple[ThreadSignal, ...]:
        if event.kind not in (
            AgentEventKind.CHAT_RECEIVED,
            AgentEventKind.SPEECH_FINAL,
            AgentEventKind.SPEECH_COMPLETED,
        ):
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
        target = self._target(event, open_threads, text)
        speaker = (
            ThreadSpeaker.VIEWER
            if event.kind is AgentEventKind.CHAT_RECEIVED else ThreadSpeaker.MAI
        )
        if event.kind is AgentEventKind.SPEECH_COMPLETED:
            if target is None:
                return ()
            move = _move(event.payload.get("conversation_move")) or target.next_move
            if _RESOLVE.search(text) or move is ConversationMove.CLOSE:
                return (ThreadSignal(
                    ThreadOperation.RESOLVE, target.kind, target.topic, target.summary,
                    evidence, target_thread_id=target.thread_id,
                    reason="delivered_completion", speaker=speaker, move=move,
                ),)
            waiting = _ends_with_question(text)
            status = (
                ThreadStatus.PARKED
                if move is ConversationMove.PARK else
                ThreadStatus.WAITING if waiting else ThreadStatus.ACTIVE
            )
            return (ThreadSignal(
                ThreadOperation.UPDATE, target.kind, target.topic, text,
                evidence, target_thread_id=target.thread_id, speaker=speaker,
                status=status,
                move=move, is_open_question=waiting,
            ),)
        if target is not None and _RESOLVE.search(text):
            return (ThreadSignal(
                ThreadOperation.RESOLVE, target.kind, target.topic, target.summary,
                evidence, target_thread_id=target.thread_id, reason="explicit_completion",
                speaker=speaker,
            ),)
        if target is not None and _FOLLOW_UP.search(text):
            return (ThreadSignal(
                ThreadOperation.UPDATE, target.kind, target.topic, text,
                evidence, target_thread_id=target.thread_id, speaker=speaker,
                status=ThreadStatus.ACTIVE,
            ),)
        if target is not None and event.kind is AgentEventKind.CHAT_RECEIVED:
            return (ThreadSignal(
                ThreadOperation.UPDATE, target.kind, target.topic, text,
                evidence, target_thread_id=target.thread_id, speaker=speaker,
                status=ThreadStatus.ACTIVE,
            ),)
        if event.kind is AgentEventKind.SPEECH_FINAL and _PROMISE.search(text):
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.PROMISE, _topic(text), text, evidence,
                speaker=speaker,
            ),)
        if "?" in text:
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.QUESTION, _topic(text), text, evidence,
                speaker=speaker,
            ),)
        if event.kind is AgentEventKind.SPEECH_FINAL and _STORY.search(text):
            return (ThreadSignal(
                ThreadOperation.CREATE, ThreadKind.STORY, _topic(text), text, evidence,
                speaker=speaker,
            ),)
        return ()

    def _target(
        self, event: GroundedEvent, open_threads: tuple[OpenThread, ...], text: str,
    ) -> OpenThread | None:
        explicit_id = str(event.payload.get("thread_id") or "").strip()
        if explicit_id:
            return next(
                (thread for thread in open_threads if thread.thread_id == explicit_id), None,
            )
        if event.kind is AgentEventKind.SPEECH_COMPLETED:
            return None
        match = self._matcher.match(text, open_threads) if self._matcher is not None else None
        if match is not None:
            return next(
                (thread for thread in open_threads if thread.thread_id == match.thread_id),
                None,
            )
        # "nãy/lúc nãy" is only a temporal reference in fast chat. Without a
        # lexical match it must not hijack whichever thread happened to be active.
        if _EXPLICIT_FOLLOW_UP.search(text) or _RESOLVE.search(text):
            candidates = tuple(
                thread for thread in open_threads if thread.status is not ThreadStatus.PARKED
            )
            if candidates:
                return max(candidates, key=lambda item: (item.updated_at, item.thread_id))
            parked = tuple(
                thread for thread in open_threads if thread.status is ThreadStatus.PARKED
            )
            if len(parked) == 1:
                return parked[0]
        return None


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


def _move(value: Any) -> ConversationMove | None:
    try:
        return ConversationMove(str(value)) if value else None
    except ValueError:
        return None


def _ends_with_question(text: str) -> bool:
    compact = " ".join(str(text).split()).rstrip('"”’)]}')
    return len(compact) >= 4 and compact.endswith("?")


def _topic(text: str) -> str:
    return " ".join(text.split())[:120]


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"
