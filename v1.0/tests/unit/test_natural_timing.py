from __future__ import annotations

from orchestrator.metrics_collector import MetricsCollector
from services.tts.natural_timing import NaturalTimingConfig, NaturalTimingPolicy


class _Pacer:
    def __init__(self) -> None:
        self.calls = 0

    def delay(self, text: str) -> float:
        self.calls += 1
        return 0.4


def _policy(metrics=None) -> NaturalTimingPolicy:
    return NaturalTimingPolicy(NaturalTimingConfig(
        min_ttfa_samples=3,
        sample_window=5,
        ttfa_ceiling_ms=1000,
        proactive_filler_only=True,
        proactive_prefixes=("self_", "room_", "trans_"),
    ), metrics=metrics)


def test_no_delay_or_filler_before_real_ttfa_samples() -> None:
    policy = _policy()
    pacer = _Pacer()
    plan = policy.plan("self_1", "hello", pacer)
    assert plan.delay_seconds == 0
    assert not plan.allow_filler
    assert plan.reason == "awaiting_real_ttfa"
    assert pacer.calls == 0


def test_calibrated_proactive_turn_can_use_pacing_and_filler() -> None:
    policy = _policy()
    for value in (300, 400, 500):
        assert policy.observe_ttfa(value)
    pacer = _Pacer()
    plan = policy.plan("self_1", "hello", pacer)
    assert plan.delay_seconds == 0.4
    assert plan.allow_filler
    assert plan.turn_kind == "proactive"


def test_chat_reply_never_uses_filler_even_after_calibration() -> None:
    policy = _policy()
    for value in (300, 400, 500):
        policy.observe_ttfa(value)
    plan = policy.plan("read_1", "viewer reply", _Pacer())
    assert plan.delay_seconds == 0.4
    assert not plan.allow_filler
    assert plan.turn_kind == "chat"


def test_slow_real_ttfa_suppresses_added_latency() -> None:
    policy = _policy()
    for value in (1200, 1400, 1600):
        policy.observe_ttfa(value)
    pacer = _Pacer()
    plan = policy.plan("self_1", "hello", pacer)
    assert plan.delay_seconds == 0
    assert not plan.allow_filler
    assert plan.reason == "ttfa_above_ceiling"
    assert pacer.calls == 0


def test_invalid_samples_do_not_unlock_policy() -> None:
    policy = _policy()
    assert not policy.observe_ttfa(None)
    assert not policy.observe_ttfa(0)
    assert not policy.snapshot()["ready"]


def test_disable_and_metrics_are_observable() -> None:
    metrics = MetricsCollector()
    policy = _policy(metrics)
    policy.plan("read_1", "hello", _Pacer())
    assert metrics.natural_timing_snapshot() == {"chat:awaiting_real_ttfa": 1}
    policy.set_enabled(False)
    assert policy.plan("self_1", "hello", _Pacer()).reason == "disabled"
