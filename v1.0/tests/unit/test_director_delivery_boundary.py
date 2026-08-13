"""Direct contract tests for the Director delivery boundary."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from interfaces.animation import MoodState
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
