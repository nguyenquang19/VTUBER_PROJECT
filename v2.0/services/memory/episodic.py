"""Bounded, privacy-safe rolling summaries for one live session.

The service decorates the A1 MemoryService chain. Source turns stay only in a
bounded RAM buffer; llama.cpp produces a meaning summary in shadow workload;
only a validated summary plus outcome provenance is written to memory.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from interfaces.base import HealthStatus
from interfaces.llm import (
    ChatMessage,
    LLMContextOverflowPolicy,
    LLMJsonSchemaResponse,
    LLMRequest,
    LLMService,
    LLMWorkloadClass,
)
from interfaces.memory import (
    EpisodicMemoryService,
    EpisodicTurn,
    MemoryEntry,
    MemoryService,
    MemoryTier,
)
from orchestrator.logger import get_logger
from services.data.sanitize import mask_pii, mask_pii_with_count
from services.memory.config import MemoryRuntimeConfig, validate_memory_query


_MEMORY_KIND = "episodic_summary"
_PII_MARKER = "[PII]"
_TIMESTAMP_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
)
_IDENTITY_RE = re.compile(
    r"\b(?:người\s*xem|viewer|bạn\s*ấy)\s+(?:tên|sinh\s*ngày|ở\s+tại)\b|"
    r"\b(?:tên\s+là|sinh\s*nhật|địa\s*chỉ|email|số\s*điện\s*thoại|cccd|cmnd|hộ\s*chiếu)\b",
    re.IGNORECASE,
)
_SYSTEM_PROMPT = (
    "Bạn tạo ký ức episodic cho Mai. Chỉ trả JSON theo schema. "
    "Tóm tắt ý nghĩa và diễn biến chung bằng tiếng Việt, không chép nguyên câu, "
    "không nêu tên, định danh, địa chỉ, liên hệ, ngày sinh, mốc giờ/ngày hoặc dữ liệu riêng tư. "
    "Không biến ký ức thành sự thật hiện tại. Salience nằm trong [0,1]."
)


class EpisodicMemoryManager(EpisodicMemoryService):
    """MemoryService wrapper owning summary cadence, TTL and episodic ranking."""

    service_id = "memory_episodic"

    def __init__(
        self,
        *,
        storage: MemoryService,
        llm: LLMService,
        session_id: str,
        config: MemoryRuntimeConfig,
        enabled: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if any(
            not callable(getattr(storage, name, None))
            for name in ("start", "stop", "health_check", "get_metrics", "write", "query", "forget")
        ):
            raise ValueError("episodic storage must implement MemoryService")
        if any(
            not callable(getattr(llm, name, None))
            for name in ("generate_stream", "cancel")
        ):
            raise ValueError("episodic llm must implement LLMService")
        if not isinstance(config, MemoryRuntimeConfig):
            raise ValueError("episodic config must be MemoryRuntimeConfig")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("episodic session_id must be non-empty")
        if not isinstance(enabled, bool):
            raise ValueError("episodic enabled must be boolean")
        self._storage = storage
        self._llm = llm
        self._session_id = session_id.strip()
        self._config = config
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._turns: deque[EpisodicTurn] = deque(maxlen=config.summary_every_turns)
        self._summary_ids: deque[str] = deque()
        self._tasks: set[asyncio.Task[None]] = set()
        self._rolling_summary: str | None = None
        self._rolling_entry_id: str | None = None
        self._counts: dict[str, int] = {}
        self._running = False
        self._log = get_logger("memory_episodic")

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        storage: MemoryService,
        llm: LLMService,
        session_id: str,
        enabled: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> "EpisodicMemoryManager":
        return cls(
            storage=storage,
            llm=llm,
            session_id=session_id,
            config=MemoryRuntimeConfig.from_loader(loader),
            enabled=enabled,
            clock=clock,
        )

    async def start(self) -> None:
        await self._storage.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self.set_enabled(False)
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for entry_id in tuple(self._summary_ids):
            try:
                await self._storage.forget(entry_id)
            except Exception as exc:
                self._record("cleanup_failed")
                self._log.warning(
                    "episodic_cleanup_failed", error=type(exc).__name__,
                )
        self._summary_ids.clear()
        self._rolling_summary = None
        self._rolling_entry_id = None
        await self._storage.stop()

    async def health_check(self) -> HealthStatus:
        storage = await self._storage.health_check()
        if not storage.is_ok:
            return HealthStatus.unhealthy(
                self.service_id, "memory storage unavailable", enabled=self._enabled,
            )
        return HealthStatus.healthy(
            self.service_id,
            enabled=self._enabled,
            pending=len(self._tasks),
            buffered_turns=len(self._turns),
            summaries=len(self._summary_ids),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            **self._storage.get_metrics(),
            "memory_episodic_enabled": self._enabled,
            "memory_episodic_observed_total": self._counts.get("observed", 0),
            "memory_episodic_generated_total": self._counts.get("generated", 0),
            "memory_episodic_rejected_total": self._counts.get("rejected", 0),
            "memory_episodic_failed_total": self._counts.get("failed", 0),
            "memory_episodic_evicted_total": self._counts.get("evicted", 0),
            "memory_episodic_expired_total": self._counts.get("expired", 0),
            "memory_episodic_retrieved_total": self._counts.get("retrieved", 0),
            "memory_episodic_backpressure_total": self._counts.get("backpressure", 0),
            "memory_episodic_cleanup_failed_total": self._counts.get("cleanup_failed", 0),
            "memory_episodic_pending": len(self._tasks),
            "memory_episodic_buffered_turns": len(self._turns),
            "memory_episodic_retained": len(self._summary_ids),
        }

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("episodic enabled must be boolean")
        self._enabled = enabled
        if enabled:
            return
        self._turns.clear()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def observe_verified_turn(self, turn: EpisodicTurn) -> bool:
        if not isinstance(turn, EpisodicTurn):
            raise ValueError("episodic observer requires EpisodicTurn")
        if not self._running or not self._enabled:
            return False
        if turn.session_id != self._session_id:
            self._record("rejected")
            return False
        self._turns.append(turn)
        self._record("observed")
        return self._schedule_ready_batch()

    def _schedule_ready_batch(self) -> bool:
        if len(self._turns) < self._config.summary_every_turns:
            return True
        if len(self._tasks) >= self._config.summary_pending_max:
            self._record("backpressure")
            return True
        batch = tuple(self._turns)
        self._turns.clear()
        try:
            task = asyncio.get_running_loop().create_task(
                self._summarize_and_store(
                    batch, self._rolling_summary, self._rolling_entry_id,
                ),
                name=f"episodic_summary_{batch[-1].outcome_id[-8:]}",
            )
        except RuntimeError:
            self._record("failed")
            return False
        self._tasks.add(task)
        task.add_done_callback(self._summary_done)
        return True

    async def write(self, entry: MemoryEntry) -> None:
        await self._storage.write(entry)

    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        query_text, top_k, tier, viewer_id = validate_memory_query(
            query_text, top_k, tier, viewer_id,
            max_top_k=self._config.max_query_top_k,
        )
        if tier in (MemoryTier.WORKING, MemoryTier.PERSISTENT):
            return await self._storage.query(query_text, top_k, tier, viewer_id)
        candidate_k = (
            self._config.max_query_top_k
            if not self._enabled
            else min(
                self._config.max_query_top_k,
                max(top_k * 3, self._config.max_summaries),
            )
        )
        candidates = await self._storage.query(
            query_text, candidate_k, tier, viewer_id,
        )
        if not self._enabled:
            return [entry for entry in candidates if not _is_episodic(entry)][:top_k]
        snapshot = getattr(self._storage, "fallback_snapshot", None)
        if callable(snapshot):
            seen = {entry.entry_id for entry in candidates}
            candidates.extend(
                entry for entry in snapshot()
                if _is_episodic(entry) and entry.entry_id not in seen
            )
        now = _utc(self._clock())
        ranked: list[tuple[float, int, MemoryEntry]] = []
        candidate_total = max(1, len(candidates))
        for index, entry in enumerate(candidates):
            relevance = 1.0 - (index / candidate_total)
            if not _is_episodic(entry):
                ranked.append((relevance, index, entry))
                continue
            if entry.metadata.get("session_id") != self._session_id:
                self._record("expired")
                continue
            expires_at = _metadata_time(entry.metadata.get("expires_at"))
            if expires_at is None or expires_at <= now:
                self._record("expired")
                continue
            recency = max(
                0.0,
                min(1.0, (expires_at - now).total_seconds() / self._config.session_ttl_s),
            )
            salience = _bounded_number(
                entry.metadata.get("summary_salience"), entry.importance,
            )
            score = (
                self._config.recency_weight * recency
                + self._config.salience_weight * salience
            )
            ranked.append((score, index, entry))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2].entry_id))
        results = [item[2] for item in ranked[:top_k]]
        self._counts["retrieved"] = self._counts.get("retrieved", 0) + sum(
            1 for entry in results if _is_episodic(entry)
        )
        return results

    async def forget(self, entry_id: str) -> None:
        await self._storage.forget(entry_id)
        try:
            self._summary_ids.remove(entry_id)
        except ValueError:
            pass
        if self._rolling_entry_id == entry_id:
            self._rolling_entry_id = None
            self._rolling_summary = None

    async def export_viewer(self, viewer_id: str) -> list[MemoryEntry]:
        return await self._storage.export_viewer(viewer_id)

    async def forget_viewer(self, viewer_id: str) -> int:
        return await self._storage.forget_viewer(viewer_id)

    def fallback_snapshot(self) -> list[MemoryEntry]:
        snapshot = getattr(self._storage, "fallback_snapshot", None)
        if not callable(snapshot):
            return []
        now = _utc(self._clock())
        result: list[MemoryEntry] = []
        for entry in snapshot():
            if not _is_episodic(entry):
                result.append(entry)
                continue
            if not self._enabled:
                continue
            expires = _metadata_time(entry.metadata.get("expires_at"))
            if entry.metadata.get("session_id") == self._session_id and expires and expires > now:
                result.append(entry)
        return result

    async def _summarize_and_store(
        self,
        batch: tuple[EpisodicTurn, ...],
        previous_summary: str | None,
        previous_entry_id: str | None,
    ) -> None:
        summary, salience = await self._generate(batch, previous_summary)
        if not self._enabled or not self._running:
            return
        refs = tuple(
            dict.fromkeys(
                ([previous_entry_id] if previous_entry_id else [])
                + [turn.outcome_id for turn in batch]
            )
        )
        entry_id = _entry_id(self._session_id, refs)
        observed_at = max(turn.timestamp for turn in batch)
        expires_at = observed_at + timedelta(seconds=self._config.session_ttl_s)
        entry = MemoryEntry(
            entry_id=entry_id,
            content=summary,
            timestamp=observed_at,
            tags=("episodic", "session_summary"),
            importance=salience,
            tier=MemoryTier.SESSION,
            metadata={
                "memory_kind": _MEMORY_KIND,
                "cognitive_kind": "EPISODIC",
                "cognitive_scope": "SESSION",
                "session_id": self._session_id,
                "expires_at": expires_at.isoformat(),
                "provenance": "verified_delivery_rollup",
                "provenance_refs": refs,
                "verified": True,
                "action_status": "delivered",
                "outcome_id": batch[-1].outcome_id,
                "confidence": 1.0,
                "summary_salience": salience,
            },
        )
        await self._storage.write(entry)
        if not self._enabled or not self._running:
            await self._storage.forget(entry.entry_id)
            return
        self._rolling_summary = summary
        self._rolling_entry_id = entry.entry_id
        self._summary_ids.append(entry.entry_id)
        self._record("generated")
        while len(self._summary_ids) > self._config.max_summaries:
            oldest = self._summary_ids.popleft()
            await self._storage.forget(oldest)
            self._record("evicted")

    async def _generate(
        self, batch: tuple[EpisodicTurn, ...], previous_summary: str | None,
    ) -> tuple[str, float]:
        request_id = _entry_id(
            self._session_id, tuple(turn.outcome_id for turn in batch), prefix="episodic_request",
        )
        request = LLMRequest(
            request_id=request_id,
            messages=[
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=_bounded_prompt(
                        previous_summary, batch, self._config.summary_input_max_chars,
                    ),
                ),
            ],
            max_tokens=self._config.summary_max_tokens,
            temperature=0.0,
            seed=self._config.summary_seed,
            workload_class=LLMWorkloadClass.SHADOW,
            context_overflow_policy=LLMContextOverflowPolicy.REJECT,
            response_format=LLMJsonSchemaResponse(
                name="mai_episodic_summary",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self._config.summary_max_chars,
                        },
                        "salience": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["summary", "salience"],
                },
            ),
        )
        chunks: list[str] = []
        saw_final = False

        async def consume() -> None:
            nonlocal saw_final
            async for token in self._llm.generate_stream(request):
                if token.is_final:
                    if saw_final:
                        raise ValueError("episodic stream emitted multiple final tokens")
                    saw_final = True
                    continue
                if token.token:
                    chunks.append(token.token)

        async with asyncio.timeout(self._config.summary_timeout_s):
            await consume()
        if not saw_final:
            raise ValueError("episodic stream ended without final token")
        raw = json.loads("".join(chunks))
        if not isinstance(raw, dict) or set(raw) != {"summary", "salience"}:
            raise ValueError("episodic response shape is invalid")
        summary = raw["summary"]
        if not isinstance(summary, str):
            raise ValueError("episodic summary must be text")
        summary = " ".join(summary.split())
        if not summary or len(summary) > self._config.summary_max_chars:
            raise ValueError("episodic summary exceeds configured bound")
        masked, pii_count = mask_pii_with_count(summary)
        if (
            pii_count
            or masked != summary
            or _PII_MARKER in summary
            or _IDENTITY_RE.search(summary)
            or _TIMESTAMP_RE.search(summary)
            or _quotes_source(summary, batch)
        ):
            self._record("rejected")
            raise ValueError("episodic summary failed privacy/meaning validation")
        salience = _bounded_number(raw["salience"], -1.0)
        if salience < 0:
            raise ValueError("episodic salience is invalid")
        source_salience = max(turn.salience for turn in batch)
        return summary, max(salience, source_salience)

    def _summary_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._record("failed")
            self._log.warning(
                "episodic_summary_failed", error=type(exc).__name__,
            )
        if self._enabled and self._running and len(self._turns) >= self._config.summary_every_turns:
            self._schedule_ready_batch()

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1


def _bounded_prompt(
    previous_summary: str | None,
    turns: Iterable[EpisodicTurn],
    max_chars: int,
) -> str:
    turn_list = tuple(turns)
    sanitized: list[tuple[str, str]] = []
    for turn in turn_list:
        user = _source_text(turn.user_text)
        assistant = _source_text(turn.assistant_text)
        sanitized.append((user, assistant))
    previous = _source_text(previous_summary or "")
    fixed = 64 + len(turn_list) * 24
    available = max(1, max_chars - fixed)
    previous_budget = min(len(previous), available // 3)
    remaining = max(1, available - previous_budget)
    field_budget = max(1, remaining // max(1, len(sanitized) * 2))
    lines = ["ROLLING_MEANING", previous[:previous_budget] or "none", "NEW_VERIFIED_TURNS"]
    for index, (user, assistant) in enumerate(sanitized, 1):
        lines.append(f"{index}. USER_MEANING: {user[:field_budget]}")
        lines.append(f"{index}. MAI_MEANING: {assistant[:field_budget]}")
    return "\n".join(lines)[:max_chars]


def _source_text(value: str) -> str:
    masked = mask_pii(value) or ""
    return " ".join(masked.replace(_PII_MARKER, "thông tin riêng tư").split())


def _quotes_source(summary: str, batch: tuple[EpisodicTurn, ...]) -> bool:
    normalized = " ".join(summary.casefold().split())
    for turn in batch:
        for source in (turn.user_text, turn.assistant_text):
            words = " ".join(source.casefold().split()).split()
            for index in range(max(0, len(words) - 7)):
                if " ".join(words[index:index + 8]) in normalized:
                    return True
    return False


def _is_episodic(entry: MemoryEntry) -> bool:
    return entry.metadata.get("memory_kind") == _MEMORY_KIND


def _metadata_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _bounded_number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return default
    return number


def _entry_id(session_id: str, refs: tuple[str, ...], *, prefix: str = "episodic") -> str:
    material = "\x1f".join((session_id, *refs)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:24]}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("episodic clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["EpisodicMemoryManager"]
