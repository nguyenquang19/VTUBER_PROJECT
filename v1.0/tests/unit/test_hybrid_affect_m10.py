from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.affect import AffectResponseMode, AffectStyle, TurnAffect
from interfaces.animation import MoodState
from orchestrator.config_loader import ConfigLoader
from services.emotion.affect_style import AffectStyleRenderer
from services.emotion.hybrid_affect import HybridAffectComposer


ROOT = Path(__file__).resolve().parents[2]


class FakeMoodStyle:
    def __init__(self) -> None:
        self.flags: set[str] = set()

    def directive_for(self, mood: MoodState, flags: set[str]) -> str | None:
        self.flags = set(flags)
        if flags:
            return None
        return f"- legacy tone {mood.dominant()}"


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


def _affect(
    mode: AffectResponseMode,
    style: AffectStyle,
) -> TurnAffect:
    return TurnAffect(
        response_mode=mode,
        style=style,
        energy=0.6,
        warmth=0.7,
        urgency=0.2,
        created_turn=0,
        expires_at_turn=1,
    )


def test_compliment_uses_soft_accept_with_legacy_tone_in_one_plan() -> None:
    mood_style = FakeMoodStyle()
    composer = HybridAffectComposer.from_loader(_loader(), mood_style=mood_style)
    plan = composer.compose(
        "chat_compliment",
        _affect(AffectResponseMode.PLAYFUL_ACCEPT, AffectStyle.TEASE),
        MoodState(nguong=8),
    )
    assert plan is not None
    assert plan.response_mode is AffectResponseMode.SOFT_ACCEPT
    assert plan.tone_source == "legacy"
    assert plan.tone_directive == "- legacy tone nguong"
    assert plan.max_sentences == 2

    rendered = AffectStyleRenderer.from_loader(_loader()).directive_for_plan(plan)
    assert rendered is not None
    assert rendered.count("legacy tone") == 1
    assert "Tối đa 2 câu" in rendered


def test_sad_share_uses_quiet_support_and_tone_flag_falls_back_to_gentle() -> None:
    mood_style = FakeMoodStyle()
    composer = HybridAffectComposer.from_loader(_loader(), mood_style=mood_style)
    plan = composer.compose(
        "chat_genuine_sad_share",
        _affect(AffectResponseMode.SUPPORTIVE, AffectStyle.GENTLE),
        MoodState(buon=9),
        {"force_gentle_tone"},
    )
    assert plan is not None
    assert plan.response_mode is AffectResponseMode.QUIET_SUPPORT
    assert plan.tone_source == "legacy"
    assert plan.tone_directive is None
    assert plan.style is AffectStyle.GENTLE
    assert mood_style.flags == {"force_gentle_tone"}


def test_compliment_without_active_legacy_tone_never_falls_back_to_tease() -> None:
    composer = HybridAffectComposer.from_loader(_loader(), mood_style=None)
    plan = composer.compose(
        "chat_compliment",
        _affect(AffectResponseMode.PLAYFUL_ACCEPT, AffectStyle.TEASE),
        MoodState(),
    )
    assert plan is not None
    assert plan.response_mode is AffectResponseMode.SOFT_ACCEPT
    assert plan.style is AffectStyle.NEUTRAL
    assert plan.tone_directive is None


@pytest.mark.parametrize(("category", "mode"), (
    ("chat_mention_direct", AffectResponseMode.QUICK_ACK),
    ("chat_insult_troll", AffectResponseMode.PLAYFUL_PUSHBACK),
    ("chat_spam_flood", AffectResponseMode.SPAM_BOUNDARY),
    ("chat_jailbreak_attempt", AffectResponseMode.PLAYFUL_DEFLECT),
    ("chat_sexual_advance", AffectResponseMode.PLAYFUL_BOUNDARY),
    ("donation_large", AffectResponseMode.CELEBRATE_GIFT),
))
def test_v2_primary_routes_keep_explicit_turn_response_mode(
    category: str, mode: AffectResponseMode,
) -> None:
    composer = HybridAffectComposer.from_loader(_loader(), mood_style=FakeMoodStyle())
    plan = composer.compose(
        category, _affect(mode, AffectStyle.SHARP), MoodState(buc=10),
    )
    assert plan is not None
    assert plan.response_mode is mode
    assert plan.tone_source == "turn"
    assert plan.tone_directive is None


def test_composer_is_observable_and_none_affect_fails_open() -> None:
    composer = HybridAffectComposer.from_loader(_loader(), mood_style=FakeMoodStyle())
    assert composer.compose("chat_spam_flood", None, MoodState()) is None
    composer.compose(
        "chat_compliment",
        _affect(AffectResponseMode.PLAYFUL_ACCEPT, AffectStyle.TEASE),
        MoodState(vui=8),
    )
    assert composer.get_metrics() == {
        "hybrid_affect_compositions_total": 1,
        "hybrid_affect_legacy_tone_uses_total": 1,
        "hybrid_affect_legacy_tone_fallbacks_total": 0,
    }


def test_invalid_route_fails_fast() -> None:
    with pytest.raises(ValueError, match="invalid tone source"):
        HybridAffectComposer(
            routes={"bad": {"tone_source": "both"}},
            default_tone_source="turn",
            default_max_sentences=2,
        )
