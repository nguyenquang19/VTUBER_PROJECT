"""Direct contract tests for the Director delivery boundary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from interfaces.animation import MoodState
from interfaces.compatibility import ActionResult, ActionStatus
from services.director.delivery_boundary import DirectorDeliveryBoundary


class RunnerStub:
    def __init__(self) -> None:
        self.finalized: list[tuple[str, bool]] = []
        self.last_filter_verdict: Any = None
        self.turn_kwargs: dict[str, Any] = {}

    def finalize_delivery(self, request_id: str, success: bool) -> None:
        self.finalized.append((request_id, success))

    async def run_turn(self, **kwargs: Any) -> str:
        self.turn_kwargs = kwargs
        return "turn"


class TransactionsStub:
    def __init__(self) -> None:
        self.stages: list[tuple[str, str]] = []

    def mark_generated(self, transaction_id: str) -> None:
        self.stages.append(("generated", transaction_id))

    def mark_delivering(self, transaction_id: str) -> None:
        self.stages.append(("delivering", transaction_id))

    def mark_delivered(self, transaction_id: str) -> None:
        self.stages.append(("delivered", transaction_id))


class AnimationStub:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def express(self, command: Any) -> None:
        self.commands.append(command)


class LoggerStub:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))


def make_boundary(
    runner: RunnerStub,
    *,
    speak: Any,
    transactions: Any = None,
    animation: Any = None,
    embodiment_policy: Any = None,
    action_adapter_boundary: Any = None,
    completed: list[tuple[Any, ...]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> DirectorDeliveryBoundary:
    completed_sink = completed if completed is not None else []
    rejected_sink = rejected if rejected is not None else []
    return DirectorDeliveryBoundary(
        runner=runner,
        speak=speak,
        transactions=transactions,
        animation=animation,
        embodiment_policy=embodiment_policy,
        action_adapter_boundary=action_adapter_boundary,
        mood_provider=lambda: MoodState(vui=4),
        speech_completed=lambda *args, **kwargs: completed_sink.append((args, kwargs)),
        filter_rejected=lambda **kwargs: rejected_sink.append(kwargs),
        logger=LoggerStub(),
    )


@pytest.mark.asyncio
async def test_successful_delivery_advances_delivery_stages_and_side_effects() -> None:
    runner = RunnerStub()
    transactions = TransactionsStub()
    animation = AnimationStub()
    completed: list[tuple[Any, ...]] = []

    async def speak(request_id: str, text: str) -> Any:
        assert (request_id, text) == ("req-1", "xin chào")
        return SimpleNamespace(delivered=True, mode="audio")

    boundary = make_boundary(
        runner,
        speak=speak,
        transactions=transactions,
        animation=animation,
        completed=completed,
    )
    reached = await boundary.deliver(
        "req-1", SimpleNamespace(text="xin chào"), "read_chat", [],
        transaction_id="tx-1",
    )

    assert reached is True
    assert transactions.stages == [
        ("generated", "tx-1"),
        ("delivering", "tx-1"),
        ("delivered", "tx-1"),
    ]
    assert runner.finalized == [("req-1", True)]
    assert len(completed) == 1
    assert animation.commands[0].mood == MoodState(vui=4)


@pytest.mark.asyncio
async def test_missing_sink_fails_after_generated_and_delivering() -> None:
    runner = RunnerStub()
    transactions = TransactionsStub()
    boundary = make_boundary(runner, speak=None, transactions=transactions)

    reached = await boundary.deliver(
        "req-2", SimpleNamespace(text="text"), "self_talk", [],
        transaction_id="tx-2",
    )

    assert reached is False
    assert transactions.stages == [
        ("generated", "tx-2"), ("delivering", "tx-2"),
    ]
    assert runner.finalized == [("req-2", False)]


@pytest.mark.asyncio
async def test_filter_rejection_is_quarantined_before_delivery() -> None:
    runner = RunnerStub()
    runner.last_filter_verdict = SimpleNamespace(
        passed=False, suggested_action="block", categories_hit=("unsafe",),
    )
    rejected: list[dict[str, Any]] = []

    async def should_not_speak(_request_id: str, _text: str) -> Any:
        raise AssertionError("filtered output must not reach the delivery sink")

    boundary = make_boundary(runner, speak=should_not_speak, rejected=rejected)
    reached = await boundary.deliver(
        "req-3", SimpleNamespace(text="unsafe"), "read_chat", ["ref"],
        thread_id="thread-1", goal_id="goal-1",
    )

    assert reached is False
    assert runner.finalized == [("req-3", False)]
    assert rejected == [{
        "refs": ["ref"], "thread_id": "thread-1", "goal_id": "goal-1",
    }]


@pytest.mark.asyncio
async def test_deferred_turn_requests_explicit_delivery_finalization() -> None:
    runner = RunnerStub()
    boundary = make_boundary(runner, speak=None)

    assert await boundary.run_turn_deferred(request_id="req-4") == "turn"
    assert runner.turn_kwargs == {
        "request_id": "req-4", "defer_delivery_commit": True,
    }


@pytest.mark.asyncio
async def test_goal_delivery_without_short_intention_fails_before_side_effect() -> None:
    runner = RunnerStub()
    calls: list[str] = []

    async def speak(request_id: str, _text: str) -> Any:
        calls.append(request_id)
        raise AssertionError("missing intention must fail before TTS")

    boundary = make_boundary(runner, speak=speak)
    assert await boundary.deliver(
        "req-missing-intention",
        SimpleNamespace(text="xin chào"),
        "continue_thread",
        [],
        goal_id="goal-1",
    ) is False
    assert calls == []
    assert runner.finalized == [("req-missing-intention", False)]
class EmbodimentStub:
    enabled = True

    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.calls: list[tuple[str, MoodState]] = []

    async def apply_mid(self, delivery_id: str, mood: MoodState) -> bool:
        self.calls.append((delivery_id, mood))
        if self.cancel:
            raise asyncio.CancelledError
        return True


@pytest.mark.asyncio
async def test_delivery_uses_embodiment_policy_only_after_confirmed_delivery() -> None:
    runner = RunnerStub()
    animation = AnimationStub()
    policy = EmbodimentStub()

    async def speak(_request_id: str, _text: str) -> Any:
        return SimpleNamespace(delivered=True, mode="audio")

    boundary = make_boundary(
        runner, speak=speak, animation=animation, embodiment_policy=policy,
    )
    assert await boundary.deliver("req-embodiment", SimpleNamespace(text="xin chào"), "read_chat", [])
    assert policy.calls == [("req-embodiment", MoodState(vui=4))]
    assert animation.commands == []


@pytest.mark.asyncio
async def test_cosmetic_cancellation_after_delivery_does_not_reverse_success() -> None:
    runner = RunnerStub()

    async def speak(_request_id: str, _text: str) -> Any:
        return SimpleNamespace(delivered=True, mode="audio")

    boundary = make_boundary(
        runner,
        speak=speak,
        animation=AnimationStub(),
        embodiment_policy=EmbodimentStub(cancel=True),
    )
    assert await boundary.deliver(
        "req-embodiment-cancel", SimpleNamespace(text="xin chào"), "read_chat", [],
    ) is True
    assert runner.finalized == [("req-embodiment-cancel", True)]


class ActionBoundaryStub:
    speech_enabled = True

    def __init__(self, *, verified: bool = True, cancel: bool = False) -> None:
        self.verified = verified
        self.cancel = cancel
        self.requests: list[Any] = []

    async def execute(self, request: Any) -> ActionResult:
        self.requests.append(request)
        if self.cancel:
            raise asyncio.CancelledError
        now = datetime.now(timezone.utc)
        return ActionResult(
            schema_version=1,
            action_id=request.action_id,
            status=ActionStatus.SUCCESS if self.verified else ActionStatus.FAILED,
            started_at=now,
            completed_at=now,
            verified=self.verified,
            verification_source="tts_delivery" if self.verified else None,
            result_data={},
            error_code=None if self.verified else "delivery_not_verified",
        )


@pytest.mark.asyncio
async def test_enabled_speech_action_boundary_verifies_before_delivered_once() -> None:
    runner = RunnerStub()
    transactions = TransactionsStub()
    animation = AnimationStub()
    adapter = ActionBoundaryStub()

    async def legacy_speak(_request_id: str, _text: str) -> Any:
        raise AssertionError("enabled adapter must own the single TTS call")

    boundary = make_boundary(
        runner,
        speak=legacy_speak,
        transactions=transactions,
        animation=animation,
        action_adapter_boundary=adapter,
    )
    reached = await boundary.deliver(
        "req-action", SimpleNamespace(text="xin chào"), "self_talk", [],
        goal_id="goal-1",
        intention_id="intention:goal-1:1",
        transaction_id="tx-action",
    )

    assert reached is True
    assert len(adapter.requests) == 1
    assert adapter.requests[0].action_type == "SELF_TALK"
    assert adapter.requests[0].intention_id == "intention:goal-1:1"
    assert adapter.requests[0].idempotency_key == "speech:req-action"
    assert transactions.stages == [
        ("generated", "tx-action"),
        ("delivering", "tx-action"),
        ("delivered", "tx-action"),
    ]
    assert animation.commands and animation.commands[0].command_type == "express"
    assert all(request.action_type != "AVATAR_GESTURE" for request in adapter.requests)


@pytest.mark.asyncio
async def test_unverified_speech_action_never_marks_delivered_or_expresses() -> None:
    runner = RunnerStub()
    transactions = TransactionsStub()
    animation = AnimationStub()
    adapter = ActionBoundaryStub(verified=False)
    boundary = make_boundary(
        runner,
        speak=None,
        transactions=transactions,
        animation=animation,
        action_adapter_boundary=adapter,
    )

    reached = await boundary.deliver(
        "req-fail", SimpleNamespace(text="không giao"), "read_chat", [],
        transaction_id="tx-fail",
    )
    assert reached is False
    assert transactions.stages == [
        ("generated", "tx-fail"), ("delivering", "tx-fail"),
    ]
    assert runner.finalized == [("req-fail", False)]
    assert animation.commands == []


@pytest.mark.asyncio
async def test_speech_action_cancellation_propagates_before_delivery() -> None:
    runner = RunnerStub()
    transactions = TransactionsStub()
    adapter = ActionBoundaryStub(cancel=True)
    boundary = make_boundary(
        runner,
        speak=None,
        transactions=transactions,
        action_adapter_boundary=adapter,
    )
    with pytest.raises(asyncio.CancelledError):
        await boundary.deliver(
            "req-cancel", SimpleNamespace(text="dừng"), "follow_up", [],
            transaction_id="tx-cancel",
        )
    assert transactions.stages == [
        ("generated", "tx-cancel"), ("delivering", "tx-cancel"),
    ]
    assert runner.finalized == []
