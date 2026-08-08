from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
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
        self._snapshot = GoalSnapshot(active=goal)

    def snapshot(self) -> GoalSnapshot:
        return self._snapshot


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
