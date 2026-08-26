from __future__ import annotations

import asyncio

import pytest

from interfaces.director_v2 import DirectorV2Proposal, DirectorV2TakeoverSelection
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.execution.local import (
    ActionAdapterConfig,
    AvatarGestureAuthority,
    AvatarGestureExecutor,
    AvatarGestureVerifier,
    LocalActionAdapterBoundary,
    SpeechDeliveryAuthority,
    SpeechDeliveryExecutor,
    SpeechDeliveryVerifier,
)
from services.execution.transaction import ActionTransactionManager
from services.director.director import DirectorAction
from tests.integration.test_director_loop import _make


def _enable_transactions(loop) -> ActionTransactionManager:
    manager = ActionTransactionManager(enabled=True)
    loop._transactions = manager
    return manager


async def _enable_speech_action_boundary(loop, speak) -> LocalActionAdapterBoundary:
    authority = SpeechDeliveryAuthority(16)
    avatar_authority = AvatarGestureAuthority(16)
    boundary = LocalActionAdapterBoundary(
        ActionAdapterConfig(1.0, 16, 4),
        speech_executor=SpeechDeliveryExecutor(speak, authority, enabled=True),
        speech_verifier=SpeechDeliveryVerifier(authority, enabled=True),
        avatar_executor=AvatarGestureExecutor(
            object(), avatar_authority, enabled=False,
        ),
        avatar_verifier=AvatarGestureVerifier(avatar_authority, enabled=False),
    )
    await boundary.start()
    loop._action_adapter_boundary = boundary
    return boundary


def _enable_v2_read_ownership(loop) -> None:
    proposal = DirectorV2Proposal(
        "p-read", 1.0, "READ_CHAT", "READ_CHAT", "m1",
        ("selected", "validated"), ("chat:m1",),
    )

    class StaticShadow:
        @staticmethod
        def propose_current() -> DirectorV2Proposal:
            return proposal

    class AcceptingSelector:
        @staticmethod
        def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
            return DirectorV2TakeoverSelection(
                True, "READ_CHAT", "accepted", "READ_CHAT", "p-read",
                "director_v2",
            )

    loop.configure_director_v2_takeover(StaticShadow(), AcceptingSelector())


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
async def test_v2_owned_read_releases_transaction_when_delivery_fails() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    _enable_v2_read_ownership(loop)
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
async def test_v2_owned_read_cancellation_releases_and_propagates() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    _enable_v2_read_ownership(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")

    async def cancel_speech(_request_id: str, _text: str) -> None:
        raise asyncio.CancelledError

    loop._speak = cancel_speech
    clock["t"] = 1.0
    with pytest.raises(asyncio.CancelledError):
        await loop.tick_once()
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1
    assert "committed" not in manager.snapshot()["counts"]


@pytest.mark.asyncio
async def test_v2_owned_duplicate_committed_read_is_not_delivered_twice() -> None:
    loop, _director, pool, _pulse, runner, clock = _make()
    manager = _enable_transactions(loop)
    _enable_v2_read_ownership(loop)
    pool.add("m1", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert runner.read_calls == ["Mai ơi"]

    pool.add("m1", "Mai ơi", now=1.1, kind="mention")
    clock["t"] = 2.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert runner.read_calls == ["Mai ơi"]
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["duplicate_committed"] == 1


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


@pytest.mark.asyncio
async def test_typed_speech_action_verification_precedes_business_commit() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    deliveries: list[str] = []

    async def subtitle(request_id: str, _text: str) -> TTSDeliveryResult:
        deliveries.append(request_id)
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    boundary = await _enable_speech_action_boundary(loop, subtitle)
    pool.add("adapter-success", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 0
    assert manager.snapshot()["counts"]["committed"] == 1
    assert len(deliveries) == 1
    assert boundary.get_metrics()["local_action_adapter_verified_total"] == 1


@pytest.mark.asyncio
async def test_partial_speech_action_never_commits_or_removes_chat() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)

    async def partial(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=False,
            mode=TTSDeliveryMode.MIXED,
            sentences_total=2,
            sentences_delivered=1,
            subtitle_sentences=1,
            failed_sentences=1,
        )

    await _enable_speech_action_boundary(loop, partial)
    pool.add("adapter-partial", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert pool.size() == 1
    assert manager.snapshot()["counts"]["released"] == 1
    assert "committed" not in manager.snapshot()["counts"]


@pytest.mark.asyncio
async def test_speech_action_duplicate_committed_never_calls_tts_twice() -> None:
    loop, _director, pool, _pulse, _runner, clock = _make()
    manager = _enable_transactions(loop)
    calls = 0

    async def subtitle(request_id: str, _text: str) -> TTSDeliveryResult:
        nonlocal calls
        calls += 1
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    await _enable_speech_action_boundary(loop, subtitle)
    pool.add("adapter-duplicate", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    pool.add("adapter-duplicate", "Mai ơi", now=1.1, kind="mention")
    clock["t"] = 2.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert calls == 1
    assert manager.snapshot()["counts"]["duplicate_committed"] == 1
