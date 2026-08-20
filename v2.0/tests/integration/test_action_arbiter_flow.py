from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from services.agent.goal_types import (
    Goal,
    GoalKind,
    GoalSnapshot,
    GoalSource,
    GoalStatus,
    ShortIntention,
    ShortIntentionStatus,
)
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.types import AgentStateSnapshot, OpenThread
from services.director.director import DirectorAction
from tests.integration.test_director_loop import _make


class FixedState:
    def __init__(self, snapshot: AgentStateSnapshot) -> None:
        self._snapshot = snapshot
        self.events = []

    def snapshot(self) -> AgentStateSnapshot:
        return self._snapshot

    def record(self, event: object) -> bool:
        self.events.append(event)
        return True


class FixedGoals:
    def __init__(self, goal: Goal) -> None:
        self.outcomes: list[tuple[str, str, str, str, str]] = []
        intention = ShortIntention(
            intention_id=f"intention:{goal.goal_id}:1",
            goal_id=goal.goal_id,
            status=ShortIntentionStatus.ACTIVE,
            step_index=0,
            step_count=len(goal.steps),
            step=goal.steps[0],
            created_at=goal.created_at,
            updated_at=goal.created_at,
            expires_at=goal.expires_at,
            reason_code="activated",
        )
        self._snapshot = GoalSnapshot(active=goal, current_intention=intention)

    def snapshot(self) -> GoalSnapshot:
        return self._snapshot

    def reconcile_threads(self, _open_thread_ids: set[str]) -> int:
        return 0

    def record_action_outcome(
        self,
        goal_id: str,
        intention_id: str,
        outcome_id: str,
        *,
        outcome: str,
        reason: str,
    ) -> bool:
        self.outcomes.append((goal_id, intention_id, outcome_id, outcome, reason))
        return True


def _goal(kind: GoalKind, now: datetime, **over: object) -> Goal:
    values: dict[str, object] = {
        "goal_id": f"goal:{kind.value}",
        "kind": kind,
        "status": GoalStatus.ACTIVE,
        "priority": 50,
        "reason": "grounded integration goal",
        "source": GoalSource.RULE,
        "created_at": now,
        "expires_at": now + timedelta(minutes=5),
        "success_conditions": ("speech_completed",),
    }
    values.update(over)
    return Goal(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_continue_thread_uses_directed_system_context_not_ambient() -> None:
    now = datetime.fromtimestamp(0.0, tz=timezone.utc)
    thread = OpenThread("thread-1", "game", "unfinished grounded topic", now, now, now + timedelta(minutes=5))
    goal = _goal(GoalKind.CONTINUE_THREAD, now, parent_thread_id="thread-1")
    state = FixedState(AgentStateSnapshot(open_threads=(thread,), active_goal_ref=goal.goal_id))
    loop, _director, _pool, _pulse, runner, clock = _make(
        agent_state=state, goal_manager=FixedGoals(goal),
    )
    clock["t"] = 1.0
    action = await loop.tick_once()
    assert action is DirectorAction.CONTINUE_THREAD
    assert len(runner.directed_calls) == 1
    assert "Thread ID: thread-1" in runner.directed_calls[0]
    assert runner.ambient_calls == []
    assert runner.read_calls == []


@pytest.mark.asyncio
async def test_tts_failure_does_not_publish_speech_completion() -> None:
    now = datetime.fromtimestamp(0.0, tz=timezone.utc)
    thread = OpenThread("thread-1", "game", "unfinished", now, now, now + timedelta(minutes=5))
    goal = _goal(GoalKind.CONTINUE_THREAD, now, parent_thread_id="thread-1")
    state = FixedState(AgentStateSnapshot(open_threads=(thread,), active_goal_ref=goal.goal_id))
    goals = FixedGoals(goal)
    loop, _director, _pool, _pulse, _runner, clock = _make(
        agent_state=state, goal_manager=goals,
    )

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("tts failed")

    loop._speak = fail_speech
    clock["t"] = 1.0
    await loop.tick_once()
    assert not any(
        getattr(getattr(event, "kind", None), "value", None) == "speech_completed"
        for event in state.events
    )
    assert goals.outcomes
    assert goals.outcomes[-1][3:] == ("failed", "not_delivered")


@pytest.mark.asyncio
async def test_authoritative_delivery_failure_terminalizes_matching_intention() -> None:
    now = datetime.fromtimestamp(0.0, tz=timezone.utc)
    thread = OpenThread(
        "thread-1", "game", "unfinished", now, now, now + timedelta(minutes=5),
    )
    goal = replace(
        _goal(GoalKind.CONTINUE_THREAD, now, parent_thread_id="thread-1"),
        status=GoalStatus.CANDIDATE,
    )
    goals = GoalManager(GoalLimits(4, 2, 8, 120), clock=lambda: now)
    assert goals.submit(goal)
    state = FixedState(AgentStateSnapshot(
        open_threads=(thread,), active_goal_ref=goal.goal_id,
    ))
    loop, _director, _pool, _pulse, _runner, clock = _make(
        agent_state=state, goal_manager=goals,
    )

    async def fail_speech(_request_id: str, _text: str) -> None:
        raise RuntimeError("tts failed")

    loop._speak = fail_speech
    clock["t"] = 1.0
    await loop.tick_once()

    snapshot = goals.snapshot()
    assert snapshot.active is None
    assert snapshot.recent_terminal[-1].status is GoalStatus.FAILED
    assert snapshot.recent_intentions[-1].status is ShortIntentionStatus.FAILED
    assert snapshot.recent_intentions[-1].reason_code == "not_delivered"


@pytest.mark.asyncio
async def test_generation_failure_does_not_publish_speech_completion() -> None:
    now = datetime.fromtimestamp(0.0, tz=timezone.utc)
    thread = OpenThread("thread-1", "game", "unfinished", now, now, now + timedelta(minutes=5))
    goal = _goal(GoalKind.CONTINUE_THREAD, now, parent_thread_id="thread-1")
    state = FixedState(AgentStateSnapshot(open_threads=(thread,), active_goal_ref=goal.goal_id))
    loop, _director, _pool, _pulse, runner, clock = _make(
        agent_state=state, goal_manager=FixedGoals(goal),
    )

    async def fail_generation(_request_id: str, _context: str):
        return type("Failed", (), {"ok": False, "text": ""})()

    runner.run_directed_turn = fail_generation
    clock["t"] = 1.0
    await loop.tick_once()
    assert not any(
        getattr(getattr(event, "kind", None), "value", None) == "speech_completed"
        for event in state.events
    )


@pytest.mark.asyncio
async def test_wait_action_never_calls_llm() -> None:
    now = datetime.fromtimestamp(0.0, tz=timezone.utc)
    goal = _goal(
        GoalKind.WAIT_FOR_CHAT_ANSWER, now,
        metadata={"question": "chat thinks what?"},
    )
    state = FixedState(AgentStateSnapshot(active_goal_ref=goal.goal_id))
    loop, _director, _pool, _pulse, runner, clock = _make(
        agent_state=state, goal_manager=FixedGoals(goal),
    )
    clock["t"] = 1.0
    action = await loop.tick_once()
    assert action is DirectorAction.WAIT
    assert runner.directed_calls == []
    assert runner.ambient_calls == []
    assert runner.read_calls == []
