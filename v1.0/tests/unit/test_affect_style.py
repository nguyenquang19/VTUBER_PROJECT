from __future__ import annotations

from pathlib import Path

from interfaces.affect import AffectResponseMode, AffectStyle, SessionMood, TurnAffect
from interfaces.animation import MoodState
from orchestrator.config_loader import ConfigLoader
from services.emotion.affect_style import AffectStyleRenderer
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager


ROOT = Path(__file__).resolve().parents[2]


def _renderer() -> AffectStyleRenderer:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return AffectStyleRenderer.from_loader(loader)


def test_renderer_is_one_line_bounded_and_has_no_raw_numbers() -> None:
    renderer = _renderer()
    directive = renderer.directive_for(
        TurnAffect(
            style=AffectStyle.GENTLE, response_mode=AffectResponseMode.SUPPORTIVE,
            energy=0.2, warmth=0.9, urgency=0.1,
            cause_ref="event:1", created_turn=0, expires_at_turn=1,
        ),
        SessionMood(valence=-0.4, updated_at=1),
    )
    assert directive is not None
    assert "\n" not in directive
    assert len(directive) <= renderer.max_chars + 2
    assert "0.9" not in directive
    assert "Công nhận cảm giác" in directive


def test_critical_modes_render_distinct_config_driven_strategies() -> None:
    renderer = _renderer()
    spam = renderer.directive_for(TurnAffect(
        style=AffectStyle.SHARP,
        response_mode=AffectResponseMode.SPAM_BOUNDARY,
        created_turn=0,
        expires_at_turn=1,
    ))
    jailbreak = renderer.directive_for(TurnAffect(
        style=AffectStyle.DEFLECT,
        response_mode=AffectResponseMode.PLAYFUL_DEFLECT,
        created_turn=0,
        expires_at_turn=1,
    ))
    assert spam is not None and "nhịp spam" in spam
    assert jailbreak is not None and "Không nhắc AI" in jailbreak
    assert spam != jailbreak


def test_prompt_uses_v2_directive_only_when_explicitly_supplied() -> None:
    pm = PromptManager(PromptCache("persona"), mood_style=None)
    legacy = pm.build_request_with_mood("legacy", "hello", MoodState(buc=8))
    cutover = pm.build_request_with_mood(
        "v2", "hello", MoodState(buc=8),
        affect_directive="- nói dịu theo affect v2",
    )
    legacy_text = "\n".join(message.content for message in legacy.messages)
    cutover_text = "\n".join(message.content for message in cutover.messages)
    assert "affect v2" not in legacy_text
    assert "affect v2" in cutover_text


def test_broken_template_fails_open_without_breaking_turn() -> None:
    renderer = AffectStyleRenderer({"gentle": "{unknown_field}"}, max_chars=100)
    affect = TurnAffect(
        style=AffectStyle.GENTLE, created_turn=0, expires_at_turn=1,
    )
    assert renderer.directive_for(affect) is None
