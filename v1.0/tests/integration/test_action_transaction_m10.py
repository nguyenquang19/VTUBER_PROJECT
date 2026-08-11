from __future__ import annotations

import pytest

from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.director.action_transaction import ActionTransactionManager
from services.director.director import DirectorAction
from tests.integration.test_director_loop import _make


def _enable_transactions(loop) -> ActionTransactionManager:
    manager = ActionTransactionManager(enabled=True)
    loop._transactions = manager
    return manager


@pytest.mark.asyncio
async def test_read_chat_is_not_removed_when_delivery_fails() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("delivery failed")

    loop._speak = fail_speech
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1
    assert "committed" not in manager.snapshot()["counts"]


@pytest.mark.asyncio
async def test_donation_reservation_survives_failed_delivery_for_retry() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add(
        "sc1", "quà nè", now=0.0, kind="chat",
        amount_vnd=500_000, is_super=True,
    )

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("tts failed")

    loop._speak = fail_speech
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.ACK_DONATION
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1


@pytest.mark.asyncio
async def test_transition_advances_only_after_delivery_success() -> None:
    loop, director, _pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("tts failed")

    loop._speak = fail_speech
    clock["t"] = 400.0
    assert await loop.tick_once() is DirectorAction.TRANSITION
    assert director.current_segment().name == "main"
    assert manager.snapshot()["counts"]["released"] == 1


@pytest.mark.asyncio
async def test_successful_delivery_commits_and_removes_chat() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 0
    assert manager.snapshot()["counts"]["committed"] == 1


@pytest.mark.asyncio
async def test_missing_speak_sink_releases_without_commit() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    loop._speak = None
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1
    assert "committed" not in manager.snapshot()["counts"]


@pytest.mark.asyncio
async def test_untyped_delivery_callback_releases_without_commit() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    async def legacy_none(_request_id: str, _text: str) -> None:
        return None

    loop._speak = legacy_none
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1
    assert "committed" not in manager.snapshot()["counts"]


@pytest.mark.asyncio
async def test_explicit_undelivered_result_releases_without_commit() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    async def no_output(_request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(request_id="m1", mode=TTSDeliveryMode.NONE)

    loop._speak = no_output
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1


@pytest.mark.asyncio
async def test_subtitle_delivery_result_commits_in_degraded_mode() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    async def subtitle(_request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id="m1", delivered=True, mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1, sentences_delivered=1, subtitle_sentences=1,
        )

    loop._speak = subtitle
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 0
    assert manager.snapshot()["counts"]["committed"] == 1
