from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.metrics_collector import MetricsCollector
from services.agent.goal_types import Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus
from services.agent.types import AgentStateSnapshot, OpenThread, ThreadEvidence, ThreadKind
from services.director.action_types import DirectorInput
from services.director.director import DirectorAction
from services.director.proactive_policy import (
    ProactiveHostingPolicy, ProactivePolicyConfig, ProactiveSource,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _policy(metrics=None) -> ProactiveHostingPolicy:
    return ProactiveHostingPolicy(ProactivePolicyConfig(
        90, "follow_up_topic", "environment_reaction", "complain_silence", 220,
    ), metrics=metrics)


def _input(state: AgentStateSnapshot, goals: GoalSnapshot = GoalSnapshot()) -> DirectorInput:
    return DirectorInput(now=100.0, agent_state=state, goals=goals)


def test_open_thread_wins_environment_and_silence_with_evidence() -> None:
    thread = OpenThread(
        "thread-1", "story", "unfinished grounded story", NOW, NOW,
        NOW + timedelta(minutes=5), kind=ThreadKind.STORY,
        evidence=(ThreadEvidence("event-story", "story excerpt", "rule"),),
    )
    state = AgentStateSnapshot(
        open_threads=(thread,),
        environment_summary={
            "salient": True, "source_event_id": "env-1", "summary": "scene changed",
        },
    )
    choice = _policy().choose(
        _input(state), allowed_actions={"follow_up", "self_talk"}, silence_ready=True,
    )
    assert choice and choice.source is ProactiveSource.OPEN_THREAD
    assert choice.action is DirectorAction.FOLLOW_UP
    assert choice.evidence_ids == ("event-story",)


def test_salient_grounded_environment_wins_silence() -> None:
    state = AgentStateSnapshot(environment_summary={
        "salient": True, "source_event_id": "env-1", "summary": "OBS switched scene",
    })
    choice = _policy().choose(
        _input(state), allowed_actions={"self_talk"}, silence_ready=True,
    )
    assert choice and choice.source is ProactiveSource.ENVIRONMENT
    assert choice.evidence_ids == ("env-1",)


def test_untrusted_environment_is_skipped_and_silence_is_last_fallback() -> None:
    state = AgentStateSnapshot(environment_summary={"salient": True, "summary": "no source"})
    choice = _policy().choose(
        _input(state), allowed_actions={"self_talk"}, silence_ready=True,
    )
    assert choice and choice.source is ProactiveSource.SILENCE


def test_active_goal_blocks_proactive_candidate() -> None:
    goal = Goal(
        "g1", GoalKind.OPERATOR_PINNED, GoalStatus.ACTIVE, 90, "operator task",
        GoalSource.OPERATOR, NOW, NOW + timedelta(minutes=5), ("complete",),
    )
    assert _policy().choose(
        _input(AgentStateSnapshot(), GoalSnapshot(active=goal)),
        allowed_actions={"self_talk"}, silence_ready=True,
    ) is None


def test_source_cooldown_and_metrics_prevent_immediate_replay() -> None:
    metrics = MetricsCollector()
    policy = _policy(metrics)
    thread = OpenThread(
        "thread-1", "story", "unfinished", NOW, NOW, NOW + timedelta(minutes=5),
    )
    value = _input(AgentStateSnapshot(open_threads=(thread,)))
    first = policy.choose(value, allowed_actions={"follow_up"}, silence_ready=False)
    assert first is not None
    policy.mark_used(first, 100.0)
    blocked = policy.choose(value, allowed_actions={"follow_up"}, silence_ready=False)
    assert blocked is not None and blocked.action is DirectorAction.WAIT
    assert metrics.proactive_candidate_snapshot()["open_thread:selected"] == 1


def test_silence_fallback_has_global_cooldown() -> None:
    policy = _policy()
    first_input = _input(AgentStateSnapshot())
    first = policy.choose(
        first_input, allowed_actions={"self_talk"}, silence_ready=True,
    )
    assert first is not None and first.source is ProactiveSource.SILENCE
    policy.mark_used(first, first_input.now)

    blocked = policy.choose(
        DirectorInput(now=120.0, agent_state=AgentStateSnapshot(), goals=GoalSnapshot()),
        allowed_actions={"self_talk"}, silence_ready=True,
    )
    assert blocked is not None and blocked.action is DirectorAction.WAIT
    assert blocked.reason == "silence_cooldown"

    ready = policy.choose(
        DirectorInput(now=145.0, agent_state=AgentStateSnapshot(), goals=GoalSnapshot()),
        allowed_actions={"self_talk"}, silence_ready=True,
    )
    assert ready is not None and ready.action is DirectorAction.SELF_TALK
