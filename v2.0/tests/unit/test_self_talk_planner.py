from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.animation import MoodState
from interfaces.self_talk import SelfTalkContext, SelfTalkStage, ThoughtCause
from orchestrator.config_loader import ConfigLoader
from orchestrator.features import FeatureManager, FeatureStatus
from services.autonomy.lore_material import LoreMaterial, LoreMaterialProvider
from services.autonomy.self_talk_planner import SelfTalkPlanner


class _MoodStyle:
    def directive_for(self, mood: MoodState, tone_flags: set[str]) -> str | None:
        return f"vui={mood.vui}; flags={','.join(sorted(tone_flags))}"


def _planner(**overrides) -> SelfTalkPlanner:
    kwargs = {
        "cognitive_moves": (
            "nhận ra một chi tiết nhỏ trong mỏ neo",
            "đặt hai cách hiểu hợp lý cạnh nhau",
            "nêu một điều chưa chắc rồi tự sửa cho chính xác",
        ),
        "mood_style": _MoodStyle(),
        "wait_for_chat_seconds": 60.0,
        "resume_after_chat_seconds": 10.0,
        "min_silence_seconds": 20.0,
        "thought_ledger_size": 3,
        "semantic_repeat_threshold": 0.72,
        "max_previous_text_chars": 80,
        "grounded_categories": ("follow_up_topic",),
        "stage_directions": {
            "open": "Mở một câu.",
            "develop": "Nối câu trước.",
            "invite": "Hỏi chat.",
        },
        "stage_limits": {
            "open": {"max_sentences": 1, "allow_question": False},
            "develop": {"max_sentences": 2, "allow_question": False},
            "invite": {"max_sentences": 2, "allow_question": True},
            "grounded": {"max_sentences": 2, "allow_question": True},
        },
    }
    kwargs.update(overrides)
    return SelfTalkPlanner(**kwargs)


def _silence(seconds: float = 30.0) -> SelfTalkContext:
    return SelfTalkContext(silence_seconds=seconds)


def _recent(text: str = "chat vừa bàn về một bài nhạc") -> SelfTalkContext:
    return SelfTalkContext(silence_seconds=30.0, recent_context=(text,))


def test_requires_a_real_cause_and_does_not_use_a_topic_pool() -> None:
    planner = _planner()
    assert planner.prepare(mood=MoodState(), now=0.0) is None

    plan = planner.prepare(
        mood=MoodState(vui=8), now=30.0, context=_silence(),
        tone_flags=("playful",),
    )
    assert plan is not None
    assert plan.cause is ThoughtCause.SILENCE
    assert plan.one_shot is True
    assert plan.evidence_refs == ("runtime:silence",)
    assert "vui=8" in plan.prompt_text
    assert "Sự thật duy nhất là khoảng im lặng" not in plan.prompt_text
    assert "topic" not in planner.snapshot()
    assert "active_topic_id" not in planner.snapshot()


def test_lore_is_grounded_before_silence_and_commits_with_delivery() -> None:
    provider = LoreMaterialProvider((LoreMaterial(
        material_id="plushies",
        section="Thích",
        anchor="Lore đã xác thực về Mai: Mai sưu tầm thú bông.",
    ),))
    planner = _planner(lore_material=provider)

    plan = planner.prepare(
        mood=MoodState(), now=30.0, context=_silence(),
    )

    assert plan is not None
    assert plan.cause is ThoughtCause.GROUNDED
    assert plan.evidence_refs == ("lore:plushies",)
    assert "sưu tầm thú bông" in plan.prompt_text
    assert provider.has_reservation("plushies")

    planner.release(plan.plan_id)
    assert not provider.has_reservation("plushies")
    retry = planner.prepare(mood=MoodState(), now=31.0, context=_silence())
    assert retry is not None and retry.evidence_refs == plan.evidence_refs
    assert planner.commit(retry.plan_id, "Tớ có cả một đội quân thú bông đấy.", 31.0)
    assert provider.get_metrics()["self_talk_lore_commits_total"] == 1


def test_lore_toggle_cancels_pending_lore_but_keeps_silence_fallback() -> None:
    provider = LoreMaterialProvider((LoreMaterial(
        material_id="sweets",
        section="Thích",
        anchor="Lore đã xác thực về Mai: Mai thích đồ ngọt.",
    ),))
    planner = _planner(lore_material=provider)
    lore_plan = planner.prepare(mood=MoodState(), now=30.0, context=_silence())
    assert lore_plan is not None

    planner.set_lore_enabled(False)

    assert planner.can_deliver(lore_plan.plan_id) is False
    fallback = planner.prepare(mood=MoodState(), now=31.0, context=_silence())
    assert fallback is not None and fallback.cause is ThoughtCause.SILENCE


def test_arc_advances_only_after_delivery_and_keeps_same_thought() -> None:
    planner = _planner()
    first = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert first and first.stage is SelfTalkStage.OPEN
    assert planner.validate_output(first.plan_id, "Tự nhiên im một lúc lại thấy đầu óc chạy lung tung.").valid

    planner.release(first.plan_id)
    retry = planner.prepare(mood=MoodState(), now=31.0, context=_recent())
    assert retry and retry.thought_id == first.thought_id
    assert retry.stage is SelfTalkStage.OPEN

    assert planner.commit(retry.plan_id, "Tự nhiên im một lúc lại thấy đầu óc chạy lung tung.", 31.0)
    develop = planner.prepare(mood=MoodState(), now=32.0, context=_recent())
    assert develop and develop.stage is SelfTalkStage.DEVELOP
    assert "Tự nhiên im một lúc" in develop.prompt_text


def test_output_shape_is_enforced_before_delivery() -> None:
    planner = _planner()
    plan = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert plan is not None
    result = planner.validate_output(
        plan.plan_id,
        "Tớ đang nghĩ này. Hay là thử cách khác? Cũng chưa chắc đâu.",
    )
    assert result.valid is False
    assert set(result.reasons) == {"too_many_sentences", "question_not_allowed"}
    assert planner.get_metrics()["self_talk_planner_output_rejected_total"] == 1


def test_chat_interrupts_pending_output_then_suspends_instead_of_erasing_arc() -> None:
    planner = _planner()
    plan = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert plan is not None
    planner.on_chat(31.0)
    assert planner.can_deliver(plan.plan_id) is False
    assert planner.snapshot()["stage"] == "open"
    assert planner.snapshot()["pending_interrupted"] is True
    planner.release(plan.plan_id)
    readiness = planner.readiness(39.0)
    assert readiness.ready is False and readiness.reason == "thought_suspended"
    assert planner.prepare(mood=MoodState(), now=39.0, context=_recent()) is None
    resumed = planner.prepare(mood=MoodState(), now=41.0, context=_recent())
    assert resumed and resumed.thought_id == plan.thought_id


def test_invite_wait_is_resolved_by_chat() -> None:
    planner = _planner()
    opened = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert opened and planner.commit(opened.plan_id, "Im một lúc cũng có cái hay.", 30.0)
    developed = planner.prepare(mood=MoodState(), now=31.0, context=_recent())
    assert developed and planner.commit(developed.plan_id, "Ít nhất tớ nghe rõ mình đang phân vân gì.", 31.0)
    invited = planner.prepare(mood=MoodState(), now=32.0, context=_recent())
    assert invited and invited.stage is SelfTalkStage.INVITE
    assert planner.validate_output(invited.plan_id, "Còn mọi người lúc im lặng thường nghĩ gì?").valid
    assert planner.commit(invited.plan_id, "Còn mọi người lúc im lặng thường nghĩ gì?", 32.0)
    assert planner.snapshot()["stage"] == "wait"
    assert planner.readiness(33.0).reason == "thought_wait_chat"
    planner.on_chat(33.0)
    assert planner.snapshot()["stage"] is None


def test_grounded_one_shot_uses_only_supplied_context_and_keeps_arc() -> None:
    planner = _planner()
    arc = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert arc is not None
    planner.release(arc.plan_id)

    grounded = planner.prepare(
        mood=MoodState(buon=8),
        now=31.0,
        base_prompt="Dữ kiện đã xác thực: viewer hỏi về trà.",
        category="follow_up_topic",
    )
    assert grounded and grounded.one_shot
    assert grounded.cause is ThoughtCause.GROUNDED
    assert "Chỉ dùng dữ kiện" in grounded.prompt_text
    assert planner.commit(grounded.plan_id, "Ừ, chuyện trà làm tớ hơi tò mò thật.", 31.0)
    resumed = planner.prepare(mood=MoodState(), now=32.0, context=_recent())
    assert resumed and resumed.thought_id == arc.thought_id


def test_every_chat_creates_global_quiet_gate_without_an_active_arc() -> None:
    planner = _planner(resume_after_chat_seconds=10.0)
    planner.on_chat(100.0)

    readiness = planner.readiness(109.9)
    assert readiness.ready is False
    assert readiness.reason == "chat_quiet_gate"
    assert planner.prepare(mood=MoodState(), now=109.9, context=_recent()) is None
    assert planner.readiness(110.0).ready is True


def test_silence_is_one_shot_until_real_chat_starts_a_new_episode() -> None:
    planner = _planner(
        cause_directions={"silence": "Chỉ nói về chính khoảng im lặng."},
    )
    first = planner.prepare(mood=MoodState(), now=30.0, context=_silence())
    assert first and first.one_shot and first.stage is SelfTalkStage.OPEN
    assert "Chỉ nói về chính khoảng im lặng" in first.prompt_text
    assert planner.commit(first.plan_id, "Im một chút cũng là một nhịp nghỉ.", 30.0)
    assert planner.snapshot()["stage"] is None
    assert planner.prepare(mood=MoodState(), now=90.0, context=_silence()) is None

    planner.on_chat(100.0)
    assert planner.prepare(mood=MoodState(), now=109.0, context=_silence()) is None
    second = planner.prepare(mood=MoodState(), now=110.0, context=_silence())
    assert second and second.one_shot


def test_context_readiness_blocks_consumed_or_missing_material_without_mutation() -> None:
    planner = _planner()
    assert planner.readiness(10.0, SelfTalkContext()).reason == "no_material"
    first = planner.prepare(mood=MoodState(), now=30.0, context=_silence())
    assert first is not None
    assert planner.commit(first.plan_id, "Im lặng cũng là một nhịp nghỉ.", 30.0)
    before = planner.snapshot()

    blocked = planner.readiness(90.0, _silence())

    assert blocked.ready is False and blocked.reason == "no_material"
    assert planner.snapshot() == before
    planner.on_chat(100.0)
    assert planner.readiness(110.0, _silence()).ready is True


def test_semantic_question_is_rejected_even_without_question_mark() -> None:
    planner = _planner()
    opened = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert opened is not None

    rhetorical = planner.validate_output(
        opened.plan_id, "Tớ cứ thấy chuyện này hơi lạ nhỉ.",
    )
    assert rhetorical.valid is False
    assert "question_not_allowed" in rhetorical.reasons

    statement = planner.validate_output(
        opened.plan_id, "Tớ thấy cách nói đó hơi lủng củng sao á.",
    )
    assert statement.valid is True

    embedded_particle = planner.validate_output(
        opened.plan_id, "Tớ thấy chuyện đó có quan trọng gì đâu nhỉ, khó hiểu thật.",
    )
    assert embedded_particle.valid is False
    assert "question_not_allowed" in embedded_particle.reasons


def test_silence_one_shot_may_use_a_grounded_rhetorical_question() -> None:
    planner = _planner(silence_allow_question=True)
    plan = planner.prepare(mood=MoodState(), now=30.0, context=_silence())
    assert plan and plan.one_shot and plan.allow_question
    verdict = planner.validate_output(plan.plan_id, "Sao tự nhiên yên thế nhỉ?")
    assert verdict.valid is True


def test_invite_requires_exactly_one_question() -> None:
    planner = _planner()
    opened = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert opened and planner.commit(
        opened.plan_id, "Tớ vừa chú ý một chi tiết trong chuyện shader.", 30.0,
    )
    developed = planner.prepare(mood=MoodState(), now=31.0, context=_recent())
    assert developed and planner.commit(
        developed.plan_id, "Có lẽ phần hiệu năng mới đáng cân nhắc.", 31.0,
    )
    invited = planner.prepare(mood=MoodState(), now=32.0, context=_recent())
    assert invited and invited.stage is SelfTalkStage.INVITE

    verdict = planner.validate_output(
        invited.plan_id, "Mọi người thấy sao? Hay nên chọn cách khác?",
    )
    assert verdict.valid is False
    assert "invitation_question_count" in verdict.reasons


def test_previous_stage_repetition_is_rejected_by_coverage() -> None:
    planner = _planner(stage_repeat_threshold=0.7, stage_repeat_min_tokens=4)
    opened = planner.prepare(mood=MoodState(), now=30.0, context=_recent())
    assert opened is not None
    assert planner.commit(
        opened.plan_id,
        "Tớ vừa chú ý câu chuyện về shader trên điện thoại.",
        30.0,
    )
    developed = planner.prepare(mood=MoodState(), now=31.0, context=_recent())
    assert developed is not None
    assert "CẤM chép lại" in developed.prompt_text

    repeated = planner.validate_output(
        developed.plan_id,
        "Tớ vừa chú ý câu chuyện về shader trên điện thoại, rồi thấy nó khá nặng.",
    )
    assert repeated.valid is False
    assert "stage_repeat" in repeated.reasons
    assert planner.get_metrics()["self_talk_planner_stage_repeat_rejected_total"] == 1


def test_ledger_is_bounded_and_detects_repeated_spoken_text() -> None:
    planner = _planner(thought_ledger_size=2)
    for index, text in enumerate(("Tớ chú ý đến trà.", "Tớ chú ý đến cà phê.")):
        plan = planner.prepare(
            mood=MoodState(), now=float(index),
            base_prompt=f"Dữ kiện đã xác thực: {text}", category="follow_up_topic",
        )
        assert plan and planner.commit(plan.plan_id, text, float(index))
    repeated = planner.prepare(
        mood=MoodState(), now=3.0,
        base_prompt="Dữ kiện đã xác thực: đồ uống.", category="follow_up_topic",
    )
    assert repeated is not None
    verdict = planner.validate_output(repeated.plan_id, "Tớ chú ý đến trà.")
    assert verdict.valid is False and "semantic_repeat" in verdict.reasons
    assert len(planner.snapshot()["ledger"]) == 2


def test_grounded_one_shot_rejects_repeat_from_same_thread_prompt() -> None:
    planner = _planner(thought_ledger_size=4)
    prompt = "Dữ kiện thread đã xác thực: cùng một chủ đề."
    first = planner.prepare(
        mood=MoodState(), now=1.0,
        base_prompt=prompt, category="follow_up_topic",
    )
    assert first is not None
    repeated_text = "Tớ vẫn thấy chi tiết này đáng để ý đấy."
    assert planner.commit(first.plan_id, repeated_text, 1.0)

    second = planner.prepare(
        mood=MoodState(), now=2.0,
        base_prompt=prompt, category="follow_up_topic",
    )
    assert second is not None
    verdict = planner.validate_output(second.plan_id, repeated_text)

    assert verdict.valid is False
    assert "semantic_repeat" in verdict.reasons


def test_emoji_only_recent_context_falls_back_to_grounded_silence() -> None:
    planner = _planner(recent_context_min_tokens=3)
    plan = planner.prepare(
        mood=MoodState(),
        now=30.0,
        context=SelfTalkContext(
            silence_seconds=30.0,
            recent_context=(":goat-turquoise-white-horns:",),
        ),
    )

    assert plan is not None
    assert plan.cause is ThoughtCause.SILENCE
    assert "Nguyên nhân ý nghĩ: silence" in plan.prompt_text
    assert "Buổi live đang có một khoảng im lặng" in plan.prompt_text
    assert planner.get_metrics()["self_talk_planner_recent_context_rejected_total"] == 1


@pytest.mark.asyncio
async def test_feature_toggle_controls_planner() -> None:
    loader = ConfigLoader(Path(__file__).resolve().parents[2] / "config")
    loader.load_all()
    manager = FeatureManager.from_config(loader)
    planner = _planner()

    async def enable() -> None:
        planner.set_enabled(True)

    async def disable() -> None:
        planner.set_enabled(False)

    manager.attach_handlers("self_talk_planner", enable=enable, disable=disable)
    lore_result = await manager.disable("self_talk_lore", user="test")
    assert lore_result.ok and lore_result.status is FeatureStatus.DISABLED
    result = await manager.disable("self_talk_planner", user="test")
    assert result.ok and result.status is FeatureStatus.DISABLED
    assert planner.enabled is False
    result = await manager.enable("self_talk_planner", user="test")
    assert result.ok and planner.enabled is True


def test_live_cognitive_moves_do_not_invite_unsupported_assumptions() -> None:
    loader = ConfigLoader(Path(__file__).resolve().parents[2] / "config")
    loader.load_all()
    planner = SelfTalkPlanner.from_loader(loader)
    moves = " ".join(planner._moves).lower()
    recent_direction = planner._cause_directions["recent_context"].lower()

    assert "thử một giả định" not in moves
    assert "nghiêng nhẹ về một cách" not in moves
    assert "không gán ý định hay hành động" in moves
    assert "không khẳng định ai đó biết" in recent_direction
