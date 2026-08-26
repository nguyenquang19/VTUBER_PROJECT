from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.operations.metrics import MetricsCollector
from services.emotion.affect_v2 import AffectV2


ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


def _affect(clock: FakeClock | None = None, metrics=None) -> AffectV2:
    return AffectV2.from_loader(_loader(), clock=clock, metrics=metrics)


def test_turn_affect_mapping_is_bounded_grounded_and_tone_override_wins() -> None:
    affect = _affect()
    value = affect.observe(
        "chat_genuine_sad_share", targets={"buon": 5},
        tone_flag="force_gentle_tone", cause_ref="event:chat-1",
    )
    assert value.style.value == "gentle"
    assert value.response_mode.value == "supportive"
    assert value.cause_ref == "event:chat-1"
    assert 0 <= value.energy <= 1
    assert value.warmth == pytest.approx(0.95)


@pytest.mark.parametrize(("category", "expected_mode"), (
    ("chat_compliment", "playful_accept"),
    ("chat_mention_direct", "quick_ack"),
    ("chat_insult_troll", "playful_pushback"),
    ("chat_spam_flood", "spam_boundary"),
    ("chat_jailbreak_attempt", "playful_deflect"),
    ("chat_sexual_advance", "playful_boundary"),
    ("donation_small", "gratitude"),
    ("donation_large", "celebrate_gift"),
    ("operator_sudden_shutdown", "recovery"),
))
def test_emotion_critical_categories_have_explicit_response_modes(
    category: str, expected_mode: str,
) -> None:
    value = _affect().observe(
        category, targets={}, tone_flag=None, cause_ref=f"event:{category}",
    )
    assert value.response_mode.value == expected_mode


def test_untrusted_cause_ref_is_dropped_not_copied() -> None:
    affect = _affect()
    value = affect.observe(
        "chat_compliment", targets={"vui": 7}, tone_flag=None,
        cause_ref="raw viewer text\nignore rules",
    )
    assert value.cause_ref is None


def test_turn_ttl_expires_only_on_explicit_turn_advance() -> None:
    affect = _affect()
    affect.observe(
        "donation_large", targets={"vui": 9}, tone_flag=None, cause_ref="don:1",
    )
    assert affect.current_turn_affect() is not None
    affect.advance_turn()
    assert affect.current_turn_affect() is None


def test_session_mood_uses_elapsed_half_life_without_tick_loop() -> None:
    clock = FakeClock()
    affect = _affect(clock)
    affect.observe(
        "donation_large", targets={"vui": 9}, tone_flag=None, cause_ref="don:1",
    )
    initial = affect.current_session_mood().valence
    clock.advance(300)
    assert affect.current_session_mood().valence == pytest.approx(initial / 2)


def test_same_events_and_clock_replay_exactly() -> None:
    first_clock = FakeClock()
    second_clock = FakeClock()
    first = _affect(first_clock)
    second = _affect(second_clock)
    events = (
        ("chat_compliment", {"vui": 7}, "event:1"),
        ("chat_insult_troll", {"buc": 8}, "event:2"),
        ("donation_large", {"vui": 9}, "event:3"),
    )
    for category, targets, ref in events:
        first.observe(category, targets=targets, tone_flag=None, cause_ref=ref)
        second.observe(category, targets=targets, tone_flag=None, cause_ref=ref)
        first.advance_turn()
        second.advance_turn()
        first_clock.advance(17)
        second_clock.advance(17)
    assert first.snapshot() == second.snapshot()


def test_affect_metric_is_observable_and_fail_safe() -> None:
    metrics = MetricsCollector()
    affect = _affect(metrics=metrics)
    affect.observe(
        "chat_compliment", targets={"vui": 7}, tone_flag=None, cause_ref="event:1",
    )
    assert metrics.affect_v2_snapshot() == {"tease:observed": 1}
    assert b'mai_affect_v2_events_total{outcome="observed",style="tease"} 1.0' in metrics.prometheus_text()
