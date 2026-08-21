from types import SimpleNamespace

from services.director.action_prompts import (
    history_text_for,
    speech_style_constraint_prompt,
    stage_direction_for,
)
from services.director.action_types import DirectorChatRef
from services.director.director import ReadMode


def _ref(*, is_owner: bool = False, is_moderator: bool = False) -> DirectorChatRef:
    return DirectorChatRef(
        msg_id="m1",
        text="thông tin vận hành",
        kind="chat",
        score=10.0,
        created_at=0.0,
        is_owner=is_owner,
        is_moderator=is_moderator,
    )


def test_direct_response_constraint_is_explicit_and_bounded() -> None:
    prompt = speech_style_constraint_prompt(
        (), avoid_question=False, max_sentences=2, max_words=32,
        direct_response=True,
    )

    assert "tối đa 2 câu và 32 từ" in prompt
    assert "Phản ứng thẳng vào ý chat" in prompt
    assert "không tóm tắt lại lời người xem" in prompt
    assert "không tự giảng giải" in prompt
    assert "Không mặc định hỏi ngược" in prompt
    assert "một câu cà khịa trực tiếp" in prompt
    assert "thay vì bịa nguyên nhân" in prompt


def test_owner_role_is_marked_for_current_stage_and_future_history() -> None:
    decision = SimpleNamespace(read_mode=ReadMode.SINGLE, refs=(_ref(is_owner=True),))

    stage = stage_direction_for(decision)
    history, commit = history_text_for(decision)

    assert "operator/chủ kênh" in stage
    assert "không phải lời Mai" in stage
    assert history == "[Nguồn: operator/chủ kênh] thông tin vận hành"
    assert commit is True


def test_moderator_role_does_not_depend_on_display_name() -> None:
    decision = SimpleNamespace(
        read_mode=ReadMode.SINGLE,
        refs=(_ref(is_moderator=True),),
    )

    assert "moderator" in (stage_direction_for(decision) or "")
    assert history_text_for(decision)[0] == "[Nguồn: moderator] thông tin vận hành"
