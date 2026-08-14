from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.types import AgentStateSnapshot, ConversationMove, OpenThread
from services.director.action_context import ActionContextBuilder, ActionContextLimits
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction, DirectorDecision


def _goal(kind: GoalKind, **over: object) -> Goal:
    now = datetime.fromtimestamp(10.0, tz=timezone.utc)
    values: dict[str, object] = {
        "goal_id": f"goal:{kind.value}",
        "kind": kind,
        "status": GoalStatus.ACTIVE,
        "priority": 50,
        "reason": "grounded reason",
        "source": GoalSource.RULE,
        "created_at": now,
        "expires_at": now + timedelta(minutes=5),
        "success_conditions": ("speech_completed",),
        "metadata": {"source_event_id": "event:1"},
    }
    values.update(over)
    return Goal(**values)  # type: ignore[arg-type]


def test_continue_thread_context_is_grounded_and_bounded() -> None:
    now = datetime.fromtimestamp(10.0, tz=timezone.utc)
    thread = OpenThread(
        "thread-1", "game", "known summary " * 80,
        now, now, now + timedelta(minutes=5),
    )
    goal = _goal(GoalKind.CONTINUE_THREAD, parent_thread_id="thread-1")
    value = DirectorInput(
        now=10.0,
        agent_state=AgentStateSnapshot(open_threads=(thread,)),
        goals=GoalSnapshot(active=goal),
    )
    decision = DirectorDecision(
        DirectorAction.CONTINUE_THREAD, "main", "continue_active_thread",
        goal_id=goal.goal_id,
    )
    context = ActionContextBuilder(ActionContextLimits(500, 80)).render(decision, value)
    assert len(context) <= 500
    assert "Thread ID: thread-1" in context
    assert "Source event ID: event:1" in context
    assert "never invent completion" in context


def test_context_rejects_stale_or_unsupported_decision() -> None:
    goal = _goal(GoalKind.OPERATOR_PINNED)
    value = DirectorInput(
        now=10.0, agent_state=AgentStateSnapshot(), goals=GoalSnapshot(active=goal),
    )
    builder = ActionContextBuilder()
    with pytest.raises(ValueError, match="active grounded goal"):
        builder.render(
            DirectorDecision(DirectorAction.SHARE_GOAL_PROGRESS, "main", "x", goal_id="old"),
            value,
        )
    with pytest.raises(ValueError, match="unsupported"):
        builder.render(
            DirectorDecision(DirectorAction.SELF_TALK, "main", "x", goal_id=goal.goal_id),
            value,
        )


@pytest.mark.parametrize(
    ("move", "expected"),
    [
        (ConversationMove.DEEPEN, "do not ask a question"),
        (ConversationMove.CLARIFY, "do not ask a question"),
        (ConversationMove.INVITE, "ask exactly one grounded question"),
        (ConversationMove.SUMMARIZE, "one short closing summary"),
        (ConversationMove.PARK, "one short closing statement"),
    ],
)
def test_continue_thread_context_enforces_move_specific_spoken_shape(
    move: ConversationMove, expected: str,
) -> None:
    now = datetime.fromtimestamp(10.0, tz=timezone.utc)
    thread = OpenThread(
        "thread-1", "game", "known summary", now, now,
        now + timedelta(minutes=5), next_move=move,
    )
    goal = _goal(GoalKind.CONTINUE_THREAD, parent_thread_id="thread-1")
    value = DirectorInput(
        now=10.0,
        agent_state=AgentStateSnapshot(open_threads=(thread,)),
        goals=GoalSnapshot(active=goal),
    )
    decision = DirectorDecision(
        DirectorAction.CONTINUE_THREAD, "main", "continue_active_thread",
        goal_id=goal.goal_id,
    )

    context = ActionContextBuilder().render(decision, value)

    assert expected in context
    assert "1-2 short sentences" in context or "one short closing" in context
