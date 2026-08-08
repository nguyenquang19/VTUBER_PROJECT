from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.types import AgentStateSnapshot, OpenThread
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.salience import SaliencePool

ROOT = Path(__file__).resolve().parents[2]


def _goal(kind: GoalKind, index: int, now: float, **over: object) -> Goal:
    created = datetime.fromtimestamp(now, tz=timezone.utc)
    values: dict[str, object] = {
        "goal_id": f"sim-goal-{index}-{kind.value}",
        "kind": kind,
        "status": GoalStatus.ACTIVE,
        "priority": 50,
        "reason": f"grounded simulation {kind.value}",
        "source": GoalSource.RULE,
        "created_at": created,
        "expires_at": created + timedelta(seconds=60),
        "success_conditions": ("matching speech_completed",),
    }
    values.update(over)
    return Goal(**values)  # type: ignore[arg-type]


def _simulate() -> dict[str, object]:
    duration_s = 3600
    event_count = 1000
    step_s = duration_s / event_count
    director = Director(
        SaliencePool(base_tier={"chat": 10}, floor=1),
        ChatPulse(),
        [Segment("main", "simulation", 7200, {
            "read_chat", "ack_donation", "self_talk", "continue_thread",
            "ask_follow_up", "share_goal_progress",
        })],
        dead_air_seconds=20,
        max_consecutive_read_chat=3,
        ask_follow_up_before_expiry_s=20,
    )
    director.start(0.0)
    actions: Counter[str] = Counter()
    goal_action_counts: Counter[str] = Counter()
    donation_latencies: list[float] = []
    goal_latencies: list[float] = []
    active: Goal | None = None
    open_threads: tuple[OpenThread, ...] = ()
    answer_next = False
    goal_instances = 0
    goal_instances_served = 0
    donation_events = 0

    for index in range(event_count):
        now = index * step_s
        if answer_next and active is not None:
            active = _goal(
                GoalKind.ANSWER_FOLLOW_UP, index, now,
                metadata={"chat_event_id": f"chat-{index}"},
                parent_thread_id=active.parent_thread_id,
            )
            answer_next = False
            goal_instances += 1

        phase = index % 125
        if active is None and phase == 20:
            dt = datetime.fromtimestamp(now, tz=timezone.utc)
            thread = OpenThread(
                f"thread-{index}", "sim topic", "grounded unfinished topic",
                dt, dt, dt + timedelta(seconds=60),
            )
            open_threads = (thread,)
            active = _goal(
                GoalKind.CONTINUE_THREAD, index, now, parent_thread_id=thread.thread_id,
            )
            goal_instances += 1
        elif active is None and phase == 50:
            active = _goal(
                GoalKind.WAIT_FOR_CHAT_ANSWER, index, now,
                expires_at=datetime.fromtimestamp(now + 10, tz=timezone.utc),
                metadata={"question": "grounded simulation question?"},
            )
            goal_instances += 1
        elif active is None and phase == 80:
            active = _goal(GoalKind.OPERATOR_PINNED, index, now, priority=90)
            goal_instances += 1

        ordinary = DirectorChatRef(
            f"chat-{index}", f"synthetic-{index}", "chat", 20.0, now,
        )
        candidates = [ordinary]
        donation_created: float | None = None
        if index % 100 == 0 or phase == 20:
            donation_created = now
            donation_events += 1
            candidates.append(DirectorChatRef(
                f"donation-{index}", "synthetic-donation", "chat", 10.0, now,
                amount_vnd=100_000, is_super=True,
            ))

        state = AgentStateSnapshot(
            open_threads=open_threads,
            active_goal_ref=active.goal_id if active else None,
        )
        value = DirectorInput(
            now=now,
            agent_state=state,
            goals=GoalSnapshot(active=active),
            chat_candidates=tuple(candidates),
            pool_size=len(candidates),
            pulse_state="normal",
            safety_hold=index > 0 and index % 137 == 0,
        )
        decision = director.decide(value)
        actions[decision.action.value] += 1
        if decision.goal_id:
            goal_action_counts[decision.goal_id] += 1

        if decision.action is DirectorAction.ACK_DONATION:
            assert donation_created is not None
            donation_latencies.append(now - donation_created)
        elif decision.action is DirectorAction.CONTINUE_THREAD and active is not None:
            goal_latencies.append(now - active.created_at.timestamp())
            goal_instances_served += 1
            active = None
            open_threads = ()
        elif decision.action is DirectorAction.ASK_FOLLOW_UP and active is not None:
            goal_latencies.append(now - active.created_at.timestamp())
            goal_instances_served += 1
            active = replace(active, metadata={**dict(active.metadata), "follow_up_asked": True})
            answer_next = True
        elif decision.action is DirectorAction.READ_CHAT and decision.goal_id and active is not None:
            goal_latencies.append(now - active.created_at.timestamp())
            goal_instances_served += 1
            active = None
        elif decision.action is DirectorAction.SHARE_GOAL_PROGRESS and active is not None:
            goal_latencies.append(now - active.created_at.timestamp())
            goal_instances_served += 1
            active = None  # deterministic operator confirmation after the progress report

        if decision.action is not DirectorAction.WAIT:
            director.mark_spoke(decision.action, now)

    return {
        "schema_version": 1,
        "duration_seconds": duration_s,
        "event_count": event_count,
        "action_counts": dict(sorted(actions.items())),
        "donation_events": donation_events,
        "donation_events_served": len(donation_latencies),
        "goal_instances": goal_instances,
        "goal_instances_served": goal_instances_served,
        "max_donation_latency_seconds": round(max(donation_latencies, default=0.0), 3),
        "max_goal_action_latency_seconds": round(max(goal_latencies, default=0.0), 3),
        "max_actions_per_goal": max(goal_action_counts.values(), default=0),
        "wait_llm_calls": 0,
        "raw_chat_included": False,
    }


def test_deterministic_one_hour_thousand_event_action_arbiter_simulation() -> None:
    summary = _simulate()
    assert summary["duration_seconds"] == 3600
    assert summary["event_count"] == 1000
    assert summary["max_donation_latency_seconds"] <= 5
    assert summary["max_goal_action_latency_seconds"] <= 10.8
    assert summary["donation_events_served"] == summary["donation_events"]
    assert summary["goal_instances_served"] == summary["goal_instances"]
    assert summary["max_actions_per_goal"] == 1
    assert summary["wait_llm_calls"] == 0
    assert summary["raw_chat_included"] is False
    artifact = json.loads(
        (ROOT / "docs" / "baselines" / "m3_action_arbiter_simulation.json").read_text(
            encoding="utf-8",
        )
    )
    assert artifact == summary
