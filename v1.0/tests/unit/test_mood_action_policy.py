from __future__ import annotations

from interfaces.animation import MoodState
from orchestrator.metrics_collector import MetricsCollector
from services.agent.goal_types import GoalKind
from services.agent.mood_policy import MoodActionPolicy, MoodPolicyConfig


def _policy(metrics=None) -> MoodActionPolicy:
    return MoodActionPolicy(MoodPolicyConfig(
        activation_floor=6,
        priority_min=0,
        priority_max=100,
        proactive_score_floor=50,
        agenda_deltas={"bon_chon": {"continue_thread": 10}},
        director_scores={"bon_chon": {"self_talk": 25}},
        tone_flag_deltas={
            "force_gentle_tone": {
                "agenda": {"answer_follow_up": 15},
                "director": {"self_talk": -30},
            },
        },
    ), metrics=metrics)


def test_strong_mood_changes_goal_priority_but_weak_mood_does_not() -> None:
    policy = _policy()
    assert policy.goal_priority(
        GoalKind.CONTINUE_THREAD, 40, MoodState(bon_chon=8), (),
    ) == 50
    assert policy.goal_priority(
        GoalKind.CONTINUE_THREAD, 40, MoodState(bon_chon=5), (),
    ) == 40


def test_tone_flag_and_mood_both_affect_real_scores() -> None:
    policy = _policy()
    assert policy.goal_priority(
        GoalKind.ANSWER_FOLLOW_UP, 70, MoodState(), {"force_gentle_tone"},
    ) == 85
    assert policy.proactive_ready(MoodState(bon_chon=8))
    assert not policy.proactive_ready(
        MoodState(bon_chon=8), {"force_gentle_tone"},
    )


def test_policy_disable_is_backward_compatible_and_metric_is_observable() -> None:
    metrics = MetricsCollector()
    policy = _policy(metrics)
    assert policy.goal_priority(
        GoalKind.CONTINUE_THREAD, 40, MoodState(bon_chon=8), (),
    ) == 50
    assert metrics.mood_adjustment_snapshot()
    policy.set_enabled(False)
    assert policy.goal_priority(
        GoalKind.CONTINUE_THREAD, 40, MoodState(bon_chon=10), (),
    ) == 40
