"""Deterministic lifecycle manager for grounded conversation threads (M4.1)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from interfaces.agent import OpenThreadManagerService
from interfaces.base import HealthStatus
from services.agent.types import (
    GroundedEvent, OpenThread, ThreadEvidence, ThreadKind, ThreadOperation, ThreadSignal,
)


@dataclass(frozen=True)
class OpenThreadLimits:
    max_open: int = 8
    ttl_seconds: float = 900.0
    evidence_max: int = 4
    field_max_chars: int = 240
    terminal_history_max: int = 32

    @classmethod
    def from_loader(cls, loader: Any) -> "OpenThreadLimits":
        prefix = "open_threads."
        value = cls(
            max_open=int(loader.get("conversation", prefix + "max_open", 8)),
            ttl_seconds=float(loader.get("conversation", prefix + "ttl_seconds", 900)),
            evidence_max=int(loader.get("conversation", prefix + "evidence_max", 4)),
            field_max_chars=int(loader.get("conversation", prefix + "field_max_chars", 240)),
            terminal_history_max=int(
                loader.get("conversation", prefix + "terminal_history_max", 32)
            ),
        )
        if min(
            value.max_open, value.ttl_seconds, value.evidence_max,
            value.field_max_chars, value.terminal_history_max,
        ) <= 0:
            raise ValueError("open thread limits must be positive")
        return value


class OpenThreadManager(OpenThreadManagerService):
    service_id = "open_thread_manager"

    def __init__(
        self,
        limits: OpenThreadLimits,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: Any = None,
        detector: Any = None,
    ) -> None:
        self.limits = limits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._detector = detector
        self._open: tuple[OpenThread, ...] = ()
        self._terminal: tuple[tuple[OpenThread, str], ...] = ()
        self._sequence = 0
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, clock: Callable[[], datetime] | None = None,
        metrics: Any = None, detector: Any = None,
    ) -> "OpenThreadManager":
        return cls(
            OpenThreadLimits.from_loader(loader), clock=clock, metrics=metrics,
            detector=detector,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, open_threads=len(self.snapshot()))

    def get_metrics(self) -> dict[str, Any]:
        return {
            **{f"thread_{key}_total": value for key, value in sorted(self._counts.items())},
            "thread_open": len(self.snapshot()),
        }

    def create(
        self,
        *,
        kind: ThreadKind,
        topic: str,
        summary: str,
        evidence: ThreadEvidence,
        thread_id: str | None = None,
    ) -> OpenThread | None:
        self.expire()
        topic = _compact(topic, self.limits.field_max_chars)
        summary = _compact(summary, self.limits.field_max_chars)
        if not topic or not summary:
            self._record("rejected", kind)
            return None
        if any(
            item.origin_event_id == evidence.source_event_id
            or (thread_id is not None and item.thread_id == thread_id)
            for item in self._open
        ):
            self._record("duplicate", kind)
            return None
        now = _utc(self._clock())
        self._sequence += 1
        identifier = thread_id or f"thread:{evidence.source_event_id}:{self._sequence:06d}"
        thread = OpenThread(
            thread_id=identifier,
            topic=topic,
            summary=summary,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.limits.ttl_seconds),
            kind=kind,
            evidence=(self._bound_evidence(evidence),),
            origin_event_id=evidence.source_event_id,
        )
        if len(self._open) >= self.limits.max_open:
            evicted = self._open[0]
            self._terminalize(evicted, "capacity")
            self._open = self._open[1:]
            self._record("expired", evicted.kind)
        self._open = (*self._open, thread)
        self._record("opened", kind)
        return thread

    def update(
        self, thread_id: str, *, summary: str, evidence: ThreadEvidence,
    ) -> bool:
        self.expire()
        thread = self._find(thread_id)
        summary = _compact(summary, self.limits.field_max_chars)
        if thread is None or not summary:
            return False
        if any(item.source_event_id == evidence.source_event_id for item in thread.evidence):
            return False
        now = _utc(self._clock())
        updated = replace(
            thread,
            summary=summary,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.limits.ttl_seconds),
            evidence=(*thread.evidence, self._bound_evidence(evidence))[-self.limits.evidence_max:],
        )
        self._open = tuple(updated if item.thread_id == thread_id else item for item in self._open)
        self._record("updated", thread.kind)
        return True

    def resolve(self, thread_id: str, *, reason: str) -> bool:
        self.expire()
        thread = self._find(thread_id)
        if thread is None:
            return False
        self._open = tuple(item for item in self._open if item.thread_id != thread_id)
        self._terminalize(thread, _compact(reason, self.limits.field_max_chars) or "resolved")
        self._record("resolved", thread.kind)
        if thread.kind is ThreadKind.PROMISE:
            self._record("promise_completed", thread.kind)
        return True

    def expire(self) -> int:
        now = _utc(self._clock())
        expired = tuple(item for item in self._open if item.expires_at <= now)
        if not expired:
            return 0
        expired_ids = {item.thread_id for item in expired}
        self._open = tuple(item for item in self._open if item.thread_id not in expired_ids)
        for thread in expired:
            self._terminalize(thread, "ttl")
            self._record("expired", thread.kind)
        return len(expired)

    def snapshot(self) -> tuple[OpenThread, ...]:
        self.expire()
        return tuple(self._open)

    def recent_terminal(self) -> tuple[tuple[OpenThread, str], ...]:
        return tuple(self._terminal)

    def handle_event(self, event: GroundedEvent) -> None:
        if self._detector is None:
            return
        for signal in self._detector.detect(event, self.snapshot()):
            self.accept_signal(signal)

    def accept_signal(self, signal: ThreadSignal) -> bool:
        if signal.operation is ThreadOperation.CREATE:
            return self.create(
                kind=signal.kind, topic=signal.topic, summary=signal.summary,
                evidence=signal.evidence,
            ) is not None
        if signal.operation is ThreadOperation.UPDATE and signal.target_thread_id:
            return self.update(
                signal.target_thread_id, summary=signal.summary, evidence=signal.evidence,
            )
        if signal.operation is ThreadOperation.RESOLVE and signal.target_thread_id:
            return self.resolve(signal.target_thread_id, reason=signal.reason or "resolved")
        return False

    def set_detector(self, detector: Any = None) -> None:
        self._detector = detector

    def _find(self, thread_id: str) -> OpenThread | None:
        return next((item for item in self._open if item.thread_id == thread_id), None)

    def _terminalize(self, thread: OpenThread, reason: str) -> None:
        self._terminal = (*self._terminal, (thread, reason))[-self.limits.terminal_history_max:]

    def _bound_evidence(self, evidence: ThreadEvidence) -> ThreadEvidence:
        return replace(evidence, excerpt=_compact(evidence.excerpt, self.limits.field_max_chars))

    def _record(self, outcome: str, kind: ThreadKind) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_thread_event"):
            try:
                self._metrics.record_thread_event(outcome, kind.value)
            except Exception:
                pass


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else text[: max(1, max_chars - 1)].rstrip() + "…"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
