from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest

from interfaces.base import HealthStatus
from interfaces.llm import LLMRequest, LLMService, LLMToken, LLMWorkloadClass
from interfaces.memory import EpisodicTurn, MemoryEntry, MemoryService, MemoryTier
from services.memory.config import MemoryRuntimeConfig
from services.memory.episodic import EpisodicMemoryManager


NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


def _config(**overrides: object) -> MemoryRuntimeConfig:
    values: dict[str, object] = {
        "working_maxlen": 20,
        "semantic_max_entries": 100,
        "query_timeout_s": 0.15,
        "latency_sample_max": 16,
        "default_top_k": 3,
        "max_query_top_k": 20,
        "content_max_chars": 4000,
        "metadata_max_items": 24,
        "metadata_text_max_chars": 512,
        "tags_max": 12,
        "tag_max_chars": 64,
        "extractor_min_chars": 15,
        "extractor_promote_intensity": 7,
        "pending_writes_max": 8,
        "summary_every_turns": 2,
        "max_summaries": 3,
        "session_ttl_s": 600.0,
        "summary_input_max_chars": 240,
        "summary_max_chars": 120,
        "summary_max_tokens": 80,
        "summary_timeout_s": 1.0,
        "summary_pending_max": 1,
        "summary_seed": 42,
        "recency_weight": 0.4,
        "salience_weight": 0.6,
    }
    values.update(overrides)
    return MemoryRuntimeConfig(**values)  # type: ignore[arg-type]


class _Storage(MemoryService):
    service_id = "fake_storage"

    def __init__(self) -> None:
        self.entries: list[MemoryEntry] = []
        self.query_entries: list[MemoryEntry] | None = None
        self.forgotten: list[str] = []
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id) if self.running else HealthStatus.stopped(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {"fake_storage_entries": len(self.entries)}

    async def write(self, entry: MemoryEntry) -> None:
        self.entries.append(entry)

    async def query(
        self,
        query_text: str,
        top_k: int = 3,
        tier: MemoryTier | None = None,
        viewer_id: str | None = None,
    ) -> list[MemoryEntry]:
        source = self.query_entries if self.query_entries is not None else self.entries
        return [entry for entry in source if tier is None or entry.tier is tier][:top_k]

    async def forget(self, entry_id: str) -> None:
        self.forgotten.append(entry_id)
        self.entries = [entry for entry in self.entries if entry.entry_id != entry_id]


class _LLM(LLMService):
    service_id = "fake_llm"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, object]:
        return {"fake_llm_requests": len(self.requests)}

    async def cancel(self, request_id: str) -> None:
        return None

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
        self.requests.append(request)
        payload = self.responses.pop(0)
        yield LLMToken(
            request_id=request.request_id,
            token=json.dumps(payload, ensure_ascii=False),
            is_final=False,
        )
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def _turn(index: int, *, user_text: str | None = None, salience: float = 0.5) -> EpisodicTurn:
    return EpisodicTurn(
        user_text=user_text or f"Người xem bàn về chủ đề giả lập số {index} trong buổi phát sóng.",
        assistant_text=f"Mai phản hồi tự nhiên cho chủ đề giả lập số {index}.",
        session_id="session-a",
        outcome_id=f"outcome-{index}",
        timestamp=NOW + timedelta(seconds=index),
        salience=salience,
    )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_verified_turns_generate_bounded_meaning_only_summary() -> None:
    storage = _Storage()
    llm = _LLM([{
        "summary": "Cuộc trò chuyện xoay quanh sở thích đồ uống và phản hồi tích cực.",
        "salience": 0.7,
    }])
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(), enabled=True, clock=lambda: NOW + timedelta(seconds=3),
    )
    await service.start()
    assert service.observe_verified_turn(_turn(1, user_text="Email tôi là test@example.com, mình thích cà phê."))
    assert service.observe_verified_turn(_turn(2))
    await _settle()

    assert len(storage.entries) == 1
    entry = storage.entries[0]
    assert entry.tier is MemoryTier.SESSION
    assert entry.content == "Cuộc trò chuyện xoay quanh sở thích đồ uống và phản hồi tích cực."
    assert entry.metadata["session_id"] == "session-a"
    assert entry.metadata["cognitive_kind"] == "EPISODIC"
    assert entry.metadata["cognitive_scope"] == "SESSION"
    assert entry.metadata["provenance_refs"] == ("outcome-1", "outcome-2")
    assert entry.metadata["action_status"] == "delivered"
    assert "test@example.com" not in str(llm.requests[0].messages[1].content)
    assert len(llm.requests[0].messages[1].content) <= 240
    assert llm.requests[0].temperature == 0.0
    assert llm.requests[0].seed == 42
    assert llm.requests[0].workload_class is LLMWorkloadClass.SHADOW
    assert service.get_metrics()["memory_episodic_generated_total"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_replay_builds_same_request_and_entry_id() -> None:
    async def replay() -> tuple[str, str]:
        storage = _Storage()
        llm = _LLM([{"summary": "Hai lượt nói về một chủ đề chung.", "salience": 0.6}])
        service = EpisodicMemoryManager(
            storage=storage, llm=llm, session_id="session-a",
            config=_config(), enabled=True, clock=lambda: NOW + timedelta(seconds=3),
        )
        await service.start()
        service.observe_verified_turn(_turn(1))
        service.observe_verified_turn(_turn(2))
        await _settle()
        result = (llm.requests[0].messages[1].content, storage.entries[0].entry_id)
        await service.stop()
        return result

    assert await replay() == await replay()


@pytest.mark.asyncio
async def test_flag_off_preserves_delegate_and_stores_no_source_turn() -> None:
    storage = _Storage()
    existing = MemoryEntry("m1", "existing", NOW)
    hidden = _summary_entry(
        "old-episodic", session="old-session", expires=NOW + timedelta(minutes=5),
        salience=1.0, observed=NOW,
    )
    storage.query_entries = [hidden, existing]
    llm = _LLM([])
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(), enabled=False, clock=lambda: NOW,
    )
    await service.start()
    assert service.observe_verified_turn(_turn(1)) is False
    assert await service.query("topic") == [existing]
    assert llm.requests == []
    assert service.get_metrics()["memory_episodic_buffered_turns"] == 0
    await service.stop()


@pytest.mark.asyncio
async def test_pii_or_timestamp_in_summary_is_rejected() -> None:
    storage = _Storage()
    llm = _LLM([{
        "summary": "Người xem dùng email leak@example.com lúc 08:30.",
        "salience": 0.8,
    }])
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(summary_every_turns=1), enabled=True, clock=lambda: NOW,
    )
    await service.start()
    service.observe_verified_turn(_turn(1))
    await _settle()
    assert storage.entries == []
    metrics = service.get_metrics()
    assert metrics["memory_episodic_rejected_total"] == 1
    assert metrics["memory_episodic_failed_total"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_max_summaries_evicts_oldest_deterministically() -> None:
    storage = _Storage()
    llm = _LLM([
        {"summary": "Ý nghĩa thứ nhất đã được khái quát.", "salience": 0.5},
        {"summary": "Ý nghĩa thứ hai tiếp nối chủ đề trước.", "salience": 0.6},
    ])
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(summary_every_turns=1, max_summaries=1),
        enabled=True, clock=lambda: NOW + timedelta(seconds=3),
    )
    await service.start()
    service.observe_verified_turn(_turn(1))
    await _settle()
    first_id = storage.entries[0].entry_id
    service.observe_verified_turn(_turn(2))
    await _settle()
    assert storage.forgotten == [first_id]
    assert len(storage.entries) == 1
    assert service.get_metrics()["memory_episodic_evicted_total"] == 1
    await service.stop()


@pytest.mark.asyncio
async def test_stop_drains_inflight_write_before_closing_storage() -> None:
    class BlockingStorage(_Storage):
        def __init__(self) -> None:
            super().__init__()
            self.write_started = asyncio.Event()
            self.release_write = asyncio.Event()

        async def write(self, entry: MemoryEntry) -> None:
            self.write_started.set()
            await self.release_write.wait()
            await super().write(entry)

    storage = BlockingStorage()
    llm = _LLM([{"summary": "Một ý nghĩa an toàn đang được lưu.", "salience": 0.5}])
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(summary_every_turns=1), enabled=True, clock=lambda: NOW,
    )
    await service.start()
    service.observe_verified_turn(_turn(1))
    await storage.write_started.wait()
    stop_task = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert stop_task.done() is False
    storage.release_write.set()
    await stop_task
    assert storage.entries == []
    assert storage.forgotten
    assert storage.running is False


@pytest.mark.asyncio
async def test_buffered_batch_drains_after_inflight_summary_completes() -> None:
    class DelayedLLM(_LLM):
        def __init__(self) -> None:
            super().__init__([
                {"summary": "Ý nghĩa thứ nhất được tạo an toàn.", "salience": 0.5},
                {"summary": "Ý nghĩa thứ hai tiếp nối an toàn.", "salience": 0.6},
            ])
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
            if not self.requests:
                self.first_started.set()
                await self.release_first.wait()
            async for token in super().generate_stream(request):
                yield token

    storage = _Storage()
    llm = DelayedLLM()
    service = EpisodicMemoryManager(
        storage=storage, llm=llm, session_id="session-a",
        config=_config(summary_every_turns=1), enabled=True, clock=lambda: NOW,
    )
    await service.start()
    service.observe_verified_turn(_turn(1))
    await llm.first_started.wait()
    service.observe_verified_turn(_turn(2))
    assert service.get_metrics()["memory_episodic_backpressure_total"] == 1
    llm.release_first.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(storage.entries) == 2
    assert service.get_metrics()["memory_episodic_generated_total"] == 2
    await service.stop()


def _summary_entry(
    entry_id: str, *, session: str, expires: datetime, salience: float, observed: datetime,
) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        content=f"summary {entry_id}",
        timestamp=observed,
        tier=MemoryTier.SESSION,
        importance=salience,
        metadata={
            "memory_kind": "episodic_summary",
            "session_id": session,
            "expires_at": expires.isoformat(),
            "summary_salience": salience,
        },
    )


@pytest.mark.asyncio
async def test_query_filters_session_and_ttl_then_ranks_recency_plus_salience() -> None:
    storage = _Storage()
    storage.query_entries = [
        _summary_entry(
            "low", session="session-a", expires=NOW + timedelta(seconds=590),
            salience=0.1, observed=NOW - timedelta(seconds=10),
        ),
        _summary_entry(
            "high", session="session-a", expires=NOW + timedelta(seconds=300),
            salience=1.0, observed=NOW - timedelta(seconds=300),
        ),
        _summary_entry(
            "expired", session="session-a", expires=NOW - timedelta(seconds=1),
            salience=1.0, observed=NOW - timedelta(seconds=601),
        ),
        _summary_entry(
            "other", session="session-b", expires=NOW + timedelta(seconds=590),
            salience=1.0, observed=NOW,
        ),
    ]
    service = EpisodicMemoryManager(
        storage=storage, llm=_LLM([]), session_id="session-a",
        config=_config(), enabled=True, clock=lambda: NOW,
    )
    await service.start()
    results = await service.query("topic", top_k=2, tier=MemoryTier.SESSION)
    assert [entry.entry_id for entry in results] == ["high", "low"]
    assert service.get_metrics()["memory_episodic_expired_total"] == 2
    assert service.get_metrics()["memory_episodic_retrieved_total"] == 2
    await service.stop()
