"""Test FallbackManager (ARCHITECTURE 8.7.7).

N1: đúng 2 level mỗi chain, không circuit breaker.
"""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.fallback_manager import (
    AllFallbacksFailedError,
    FallbackManager,
    UnknownChainError,
)


class EventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, payload: dict[str, object]) -> None:
        self.events.append((topic, payload))


def ok_handler(value):
    async def _h(request):
        return value
    return _h


def failing_handler(exc=RuntimeError("boom")):
    async def _h(request):
        raise exc
    return _h


def slow_handler(delay, value="slow"):
    async def _h(request):
        await asyncio.sleep(delay)
        return value
    return _h


class TestRegistration:
    def test_requires_exactly_two_levels(self) -> None:
        fm = FallbackManager()
        with pytest.raises(ValueError, match="đúng 2 level"):
            fm.register_chain("llm", [ok_handler("a")], [5.0])
        with pytest.raises(ValueError, match="đúng 2 level"):
            fm.register_chain("llm", [ok_handler("a"), ok_handler("b"), ok_handler("c")], [1, 1, 1])

    def test_timeout_count_must_match(self) -> None:
        fm = FallbackManager()
        with pytest.raises(ValueError, match="số timeout"):
            fm.register_chain("llm", [ok_handler("a"), ok_handler("b")], [5.0])

    def test_has_chain(self) -> None:
        fm = FallbackManager()
        assert fm.has_chain("llm") is False
        fm.register_chain("llm", [ok_handler("a"), ok_handler("b")], [5.0, 0.1])
        assert fm.has_chain("llm") is True


class TestExecute:
    async def test_primary_success(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [ok_handler("primary"), ok_handler("canned")], [5.0, 0.1])
        result = await fm.execute("llm", request=None)
        assert result.value == "primary"
        assert result.level_used == 0
        assert result.attempts == 1

    async def test_falls_to_level_2_on_primary_failure(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [failing_handler(), ok_handler("canned")], [5.0, 0.1])
        result = await fm.execute("llm", request=None)
        assert result.value == "canned"
        assert result.level_used == 1
        assert result.attempts == 2

    async def test_falls_to_level_2_on_timeout(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [slow_handler(1.0), ok_handler("canned")], [0.05, 0.1])
        result = await fm.execute("llm", request=None)
        assert result.value == "canned"
        assert result.level_used == 1

    async def test_all_levels_fail_raises(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [failing_handler(), failing_handler()], [5.0, 0.1])
        with pytest.raises(AllFallbacksFailedError) as exc:
            await fm.execute("llm", request=None)
        assert exc.value.module_id == "llm"
        assert len(exc.value.errors) == 2

    async def test_unknown_chain_raises(self) -> None:
        fm = FallbackManager()
        with pytest.raises(UnknownChainError):
            await fm.execute("nonexistent", request=None)

    async def test_request_passed_to_handler(self) -> None:
        fm = FallbackManager()
        seen = []

        async def capture(request):
            seen.append(request)
            return "ok"

        fm.register_chain("llm", [capture, ok_handler("x")], [5.0, 0.1])
        await fm.execute("llm", request={"prompt": "hi"})
        assert seen == [{"prompt": "hi"}]


class TestMetricsAndEvents:
    async def test_fallback_count_incremented(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [failing_handler(), ok_handler("canned")], [5.0, 0.1])
        await fm.execute("llm", request=None)
        await fm.execute("llm", request=None)
        metrics = fm.get_metrics()
        assert metrics["fallback_triggered_total.llm"] == 2
        assert metrics["fallback_chains"] == 1

    async def test_no_fallback_no_count(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [ok_handler("primary"), ok_handler("canned")], [5.0, 0.1])
        await fm.execute("llm", request=None)
        assert "fallback_triggered_total.llm" not in fm.get_metrics()

    async def test_publishes_fallback_event(self) -> None:
        bus = EventSink()
        fm = FallbackManager(event_bus=bus)
        fm.register_chain("tts", [failing_handler(), ok_handler("subtitle")], [3.0, 0.1])
        await fm.execute("tts", request=None)
        topic, event = bus.events[-1]
        assert topic == "fallback_triggered"
        assert event["module"] == "tts"
        assert event["level"] == 0

    async def test_timeout_reason_labeled(self) -> None:
        bus = EventSink()
        fm = FallbackManager(event_bus=bus)
        fm.register_chain("llm", [slow_handler(1.0), ok_handler("canned")], [0.05, 0.1])
        await fm.execute("llm", request=None)
        topic, event = bus.events[-1]
        assert topic == "fallback_triggered"
        assert event["reason"] == "timeout"


class TestTwoLevelChainsMatchSpec:
    """8.7.7: llm/tts/stt đều 2 level. Skeleton cho đăng ký đúng shape đó."""

    async def test_llm_chain_shape(self) -> None:
        fm = FallbackManager()
        fm.register_chain("llm", [ok_handler("gemma"), ok_handler("canned")], [5.0, 0.1])
        result = await fm.execute("llm", None)
        assert result.value == "gemma"

    async def test_tts_chain_shape(self) -> None:
        fm = FallbackManager()
        fm.register_chain("tts", [ok_handler("vieneu"), ok_handler("subtitle")], [3.0, 0.1])
        result = await fm.execute("tts", None)
        assert result.value == "vieneu"
