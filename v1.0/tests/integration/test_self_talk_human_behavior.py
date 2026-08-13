"""Deterministic behavioral simulation for cause-first self-talk."""
from __future__ import annotations

from pathlib import Path

from interfaces.animation import MoodState
from interfaces.self_talk import SelfTalkContext, ThoughtCause
from orchestrator.config_loader import ConfigLoader
from services.autonomy.lore_material import LoreMaterialProvider
from services.autonomy.self_talk_planner import SelfTalkPlanner


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load() -> tuple[SelfTalkPlanner, dict]:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    return SelfTalkPlanner.from_loader(loader), loader.get("self_talk", "self_talk", {})


def test_repository_config_has_cognitive_moves_but_no_semantic_topic_pool() -> None:
    planner, raw = _load()
    assert "topics" not in raw
    assert len(raw["cognitive_moves"]) >= 3
    assert planner.health_check is not None


def test_cause_priority_is_grounded_environment_context_then_silence() -> None:
    planner, _ = _load()
    context = SelfTalkContext(
        silence_seconds=120.0,
        recent_context=("chat vừa bàn về nhạc",),
        environment_summary="màn hình đang ở menu đã xác thực",
    )
    environment = planner.prepare(mood=MoodState(), now=120.0, context=context)
    assert environment and environment.cause is ThoughtCause.ENVIRONMENT
    planner.release(environment.plan_id)

    planner2, _ = _load()
    recent = planner2.prepare(
        mood=MoodState(), now=120.0,
        context=SelfTalkContext(
            silence_seconds=120.0, recent_context=("chat vừa bàn về nhạc",),
        ),
    )
    assert recent and recent.cause is ThoughtCause.RECENT_CONTEXT

    planner3, _ = _load()
    silence = planner3.prepare(
        mood=MoodState(), now=120.0,
        context=SelfTalkContext(silence_seconds=120.0),
    )
    assert silence and silence.cause is ThoughtCause.SILENCE


def test_silence_is_one_safe_one_shot_per_quiet_episode() -> None:
    planner, _ = _load()
    context = SelfTalkContext(silence_seconds=120.0)
    first = planner.prepare(mood=MoodState(), now=120.0, context=context)
    assert first and first.one_shot
    assert "CẤM mô tả phòng" in first.prompt_text
    assert planner.commit(first.plan_id, "Yên một chút cũng dễ chịu.", 120.0)
    assert planner.prepare(mood=MoodState(), now=300.0, context=context) is None
    assert planner.get_metrics()["self_talk_planner_silence_one_shots_total"] == 1

    planner.on_chat(301.0)
    second = planner.prepare(mood=MoodState(), now=313.0, context=context)
    assert second and second.one_shot


def test_mood_changes_expression_directive_not_cause_or_evidence() -> None:
    class Style:
        def directive_for(self, mood: MoodState, _flags: set[str]) -> str:
            return f"vui={mood.vui};buon={mood.buon}"

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    calm = SelfTalkPlanner.from_loader(loader, mood_style=Style())
    bright = SelfTalkPlanner.from_loader(loader, mood_style=Style())
    context = SelfTalkContext(recent_context=("chat vừa nói về một bài nhạc",))
    calm_plan = calm.prepare(mood=MoodState(buon=7), now=1.0, context=context)
    bright_plan = bright.prepare(mood=MoodState(vui=8), now=1.0, context=context)
    assert calm_plan and bright_plan
    assert calm_plan.thought_id == bright_plan.thought_id
    assert calm_plan.cause == bright_plan.cause
    assert calm_plan.evidence_refs == bright_plan.evidence_refs
    assert "buon=7" in calm_plan.prompt_text
    assert "vui=8" in bright_plan.prompt_text
    assert "CẤM bịa" in calm_plan.prompt_text


def test_repository_lore_self_talk_is_grounded_and_delivery_transactional() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    lore = LoreMaterialProvider.from_loader(loader)
    planner = SelfTalkPlanner.from_loader(loader, lore_material=lore)
    context = SelfTalkContext(silence_seconds=120.0)

    first = planner.prepare(mood=MoodState(), now=120.0, context=context)
    assert first is not None
    assert first.cause is ThoughtCause.GROUNDED
    assert first.evidence_refs[0].startswith("lore:")
    assert "Lore đã xác thực về Mai" in first.prompt_text

    planner.release(first.plan_id)
    retry = planner.prepare(mood=MoodState(), now=121.0, context=context)
    assert retry is not None and retry.evidence_refs == first.evidence_refs
    assert planner.commit(retry.plan_id, "Tớ có cả một đội quân thú bông đấy.", 121.0)
    metrics = planner.get_metrics()
    assert metrics["self_talk_lore_releases_total"] == 1
    assert metrics["self_talk_lore_commits_total"] == 1
