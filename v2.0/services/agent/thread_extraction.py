"""Optional strict-schema post-hoc thread extraction via llama.cpp (M4.2)."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from interfaces.agent import ThreadExtractionService
from interfaces.base import HealthStatus
from interfaces.llm import ChatMessage, LLMRequest
from interfaces.events import AgentEventKind, GroundedEvent
from interfaces.state import (
    AgentStateSnapshot, ThreadEvidence, ThreadExtraction, ThreadKind, ThreadOperation,
    ThreadSignal,
)


class PostHocThreadExtractor(ThreadExtractionService):
    service_id = "thread_extraction"

    def __init__(
        self,
        llm: Any,
        system_prompt: str,
        *,
        max_tokens: int = 180,
        temperature: float = 0.0,
        evidence_max_items: int = 6,
        field_max_chars: int = 240,
        enabled: bool = False,
        metrics: Any = None,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._evidence_max_items = evidence_max_items
        self._field_max_chars = field_max_chars
        self._enabled = bool(enabled)
        self._metrics = metrics
        self._running = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, llm: Any, *, enabled: bool = False, metrics: Any = None,
    ) -> "PostHocThreadExtractor":
        prompt = (
            Path(__file__).resolve().parents[2]
            / "config" / "prompts" / "thread_extraction_system.txt"
        ).read_text(encoding="utf-8")
        prefix = "extraction."
        return cls(
            llm, prompt,
            max_tokens=int(loader.get("conversation", prefix + "max_tokens", 180)),
            temperature=float(loader.get("conversation", prefix + "temperature", 0.0)),
            evidence_max_items=int(
                loader.get("conversation", prefix + "evidence_max_items", 6)
            ),
            field_max_chars=int(
                loader.get("conversation", prefix + "field_max_chars", 240)
            ),
            enabled=enabled,
            metrics=metrics,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self._enabled)

    def get_metrics(self) -> dict[str, Any]:
        return {
            **{f"thread_extraction_{key}_total": value for key, value in self._counts.items()},
            "thread_extraction_enabled": self._enabled,
        }

    async def propose(
        self, event: GroundedEvent, state: AgentStateSnapshot,
    ) -> ThreadExtraction | None:
        if not self._enabled:
            self._record("disabled")
            return None
        if event.kind not in (AgentEventKind.CHAT_RECEIVED, AgentEventKind.SPEECH_FINAL):
            self._record("unsupported_event")
            return None
        text = str(event.payload.get("text") or "")
        evidence = {
            "current_event": {
                "event_id": event.event_id,
                "kind": event.kind.value,
                "text": text[: self._field_max_chars],
            },
            "recent_events": [
                {
                    "event_id": item.event_id,
                    "kind": item.kind.value,
                    "text": str(item.payload.get("text") or "")[: self._field_max_chars],
                }
                for item in state.recent_events[-self._evidence_max_items:]
            ],
            "open_threads": [
                {
                    "thread_id": item.thread_id,
                    "kind": item.kind.value,
                    "summary": item.summary[: self._field_max_chars],
                }
                for item in state.open_threads
            ],
        }
        request = LLMRequest(
            request_id=f"thread_extract_{uuid.uuid4().hex[:12]}",
            messages=[
                ChatMessage(role="system", content=self._system_prompt),
                ChatMessage(role="user", content=json.dumps(evidence, ensure_ascii=False)),
            ],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        chunks: list[str] = []
        try:
            async for token in self._llm.generate_stream(request):
                if token.token:
                    chunks.append(token.token)
            data = json.loads("".join(chunks).strip())
            if data == {}:
                self._record("empty")
                return None
            extraction = ThreadExtraction.model_validate(data)
            if not self._grounded(extraction, event, state):
                self._record("ungrounded")
                return None
            self._record("accepted")
            return extraction
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            self._record("invalid_schema")
            return None
        except Exception:
            self._record("error")
            return None

    def observe(
        self, event: GroundedEvent, state: AgentStateSnapshot, manager: Any,
    ) -> None:
        if not self._enabled or not self._running:
            return
        task = asyncio.create_task(
            self._observe(event, state, manager), name=f"thread_extract:{event.event_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _observe(
        self, event: GroundedEvent, state: AgentStateSnapshot, manager: Any,
    ) -> None:
        extraction = await self.propose(event, state)
        if extraction is not None:
            manager.accept_signal(_to_signal(extraction, event.confidence))

    def _grounded(
        self, extraction: ThreadExtraction, event: GroundedEvent,
        state: AgentStateSnapshot,
    ) -> bool:
        if extraction.source_event_id != event.event_id:
            return False
        source_text = " ".join(str(event.payload.get("text") or "").casefold().split())
        excerpt = " ".join(extraction.evidence_excerpt.casefold().split())
        if not excerpt or excerpt not in source_text:
            return False
        targets = {item.thread_id for item in state.open_threads}
        if extraction.operation is ThreadOperation.CREATE:
            return extraction.target_thread_id is None
        return extraction.target_thread_id in targets

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_thread_event"):
            try:
                self._metrics.record_thread_event(f"extraction_{outcome}", "post_hoc")
            except Exception:
                pass


def _to_signal(extraction: ThreadExtraction, confidence: float) -> ThreadSignal:
    return ThreadSignal(
        operation=extraction.operation,
        kind=extraction.kind,
        topic=extraction.topic,
        summary=extraction.summary,
        evidence=ThreadEvidence(
            extraction.source_event_id,
            extraction.evidence_excerpt,
            "llm_post_hoc",
            confidence,
        ),
        target_thread_id=extraction.target_thread_id,
        reason=extraction.reason,
    )
