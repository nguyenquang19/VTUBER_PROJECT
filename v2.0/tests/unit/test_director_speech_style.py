from services.director.speech_style import (
    SpeechStyleGuard,
    looks_like_question,
    summarize_speech_style,
)
from services.director.action_prompts import (
    literal_grounding_directive,
    speech_style_constraint_prompt,
    speech_style_correction_prompt,
)


def _guard(**overrides: object) -> SpeechStyleGuard:
    values: dict[str, object] = {
        "recent_window": 3,
        "formula_openers": ("mà", "trời ơi", "ủa", "ơ kìa"),
        "max_formula_openers": 1,
        "max_same_opener": 1,
        "max_questions": 1,
        "question_endings": ("nhỉ", "hả", "sao", "nào"),
    }
    values.update(overrides)
    return SpeechStyleGuard(**values)  # type: ignore[arg-type]


def test_opener_matching_ignores_case_and_leading_punctuation() -> None:
    guard = _guard()

    assert guard.opener_for('  “TRỜI ƠI, căng vậy!”') == "trời ơi"
    assert guard.opener_for("Màu này đẹp đấy.") is None


def test_formula_and_same_opener_budgets_are_separate() -> None:
    guard = _guard(max_formula_openers=2)
    guard.record("Mà chuyện này ổn rồi.")

    same = guard.assess("Mà phần kia để sau.")
    different = guard.assess("Ủa, phần kia để sau.")

    assert same.reasons == ("same_opener_budget",)
    assert different.valid is True


def test_formula_group_budget_blocks_rotation_between_openers() -> None:
    guard = _guard(max_formula_openers=2)
    guard.record("Mà chuyện này ổn rồi.")
    guard.record("Ủa, chuyện kia cũng ổn.")

    assessment = guard.assess("Ơ kìa, lại thêm chuyện nữa.")

    assert "formula_opener_budget" in assessment.reasons


def test_question_budget_detects_semantic_ending_and_invite_bypasses() -> None:
    guard = _guard()
    guard.record("Cậu thấy đoạn này ổn không nhỉ.")

    blocked = guard.assess("Kể tiếp được không nào.")
    invite = guard.assess(
        "Kể tiếp được không nào.", question_budget_exempt=True,
    )

    assert blocked.reasons == ("question_budget",)
    assert invite.valid is True


def test_invite_exempts_only_question_budget() -> None:
    guard = _guard()
    guard.record("Mà chuyện này ổn rồi.")
    guard.record("Cậu thấy đoạn này ổn không nhỉ.")

    assessment = guard.assess(
        "Mà cậu muốn kể tiếp không nào.", question_budget_exempt=True,
    )

    assert assessment.reasons == (
        "formula_opener_budget", "same_opener_budget",
    )
    assert assessment.question_like is True


def test_constraints_reflect_only_exhausted_budgets() -> None:
    guard = _guard(max_formula_openers=2)
    guard.record("Mà chuyện này ổn rồi.")
    guard.record("Cậu thích phần nào?")

    forbidden, avoid_question = guard.constraints()

    assert forbidden == ("mà",)
    assert avoid_question is True


def test_recent_style_state_is_bounded() -> None:
    guard = _guard(recent_window=2, max_formula_openers=2, max_questions=2)
    guard.record("Một.")
    guard.record("Hai.")
    guard.record("Ba.")

    assert guard.snapshot() == ((None, False), (None, False))
    assert guard.recent_count() == 2


def test_style_summary_uses_same_production_normalisation() -> None:
    summary = summarize_speech_style(
        ["Mà được rồi.", "Ổn đấy.", "Cậu nghĩ sao?"],
        formula_openers=("mà", "ủa"),
        question_endings=("sao",),
    )

    assert summary.total == 3
    assert summary.formula_openers == 1
    assert summary.questions == 1
    assert summary.formula_opener_ratio == 1 / 3
    assert looks_like_question("Cậu nghĩ sao.", ("sao",)) is True


def test_shape_budget_and_clamp_keep_complete_early_sentences() -> None:
    guard = _guard(max_sentences=2, max_words=12)
    text = (
        "Câu đầu đủ ý rồi. Câu thứ hai vẫn vừa đủ. "
        "Câu thứ ba lặp lại và phải bỏ."
    )

    assessment = guard.assess(text)
    clamped = guard.clamp_shape(text)

    assert "sentence_budget" in assessment.reasons
    assert "word_budget" in assessment.reasons
    assert clamped == "Câu đầu đủ ý rồi. Câu thứ hai vẫn vừa đủ."
    assert guard.assess(clamped).valid is True


def test_formula_phrase_budget_uses_only_delivered_recent_speech() -> None:
    guard = _guard(
        formula_phrases=("làm tớ thấy", "rồi đấy"),
        max_formula_phrases=1,
    )
    assert guard.assess("Câu này làm tớ thấy vui.").valid is True

    guard.record("Câu trước làm tớ thấy vui.")
    assessment = guard.assess("Đoạn này ổn rồi đấy.")

    assert assessment.reasons == ("formula_phrase_budget",)
    assert assessment.phrase == "rồi đấy"
    assert guard.forbidden_formula_phrases() == ("làm tớ thấy", "rồi đấy")


def test_language_integrity_fragment_is_bounded_and_word_aware() -> None:
    guard = _guard(language_integrity_fragments=("kalau", "ghêó", "тут", "สัก"))

    contaminated = guard.assess("Nhưng kalau là tớ thì sẽ chờ.")
    malformed = guard.assess("Tớ thấy vui ghêó.")

    assert contaminated.reasons == ("language_integrity",)
    assert contaminated.language_fragment == "kalau"
    assert malformed.language_fragment == "ghêó"
    assert guard.assess("Biết rồi khổ lắm тут nói mãi.").language_fragment == "тут"
    assert guard.assess("Tớ muốn nuôi đượcสัก con.").language_fragment == "สัก"
    assert guard.assess("Tên Kalauton nghe lạ.").valid is True


def test_vague_grounding_is_source_aware_and_word_bounded() -> None:
    guard = _guard(
        vague_input_max_words=1,
        vague_grounding_forbidden_patterns=("chắc chắn", "âm mưu"),
    )

    blocked = guard.assess(
        "Cậu chắc chắn đang có âm mưu.", grounding_text=":)",
    )

    assert blocked.reasons == ("vague_grounding",)
    assert blocked.grounding_pattern == "chắc chắn"
    assert guard.assess(
        "Chuyện này chắc chắn ổn.", grounding_text="chắc chắn",
    ).valid is True


def test_malformed_token_guard_is_source_aware_and_allows_known_terms() -> None:
    guard = _guard(
        malformed_token_fragments=("ghêó", "thiệt da"),
        malformed_token_allowlist=("OpenAI", "YouTube"),
        malformed_mixed_case_min_prefix_chars=3,
    )

    exact = guard.assess("Câu này nghe thiệt da.")
    mixed = guard.assess("Đừng để cái mặt nghClient trăn trở.")

    assert exact.reasons == ("malformed_token",)
    assert exact.malformed_token == "thiệt da"
    assert mixed.malformed_token == "nghClient"
    assert guard.assess("OpenAI và YouTube đều là tên riêng.").valid is True
    assert guard.assess(
        "Cậu vừa nhắc nghClient.", grounding_text="nghClient",
    ).valid is True


def test_semantic_over_inference_is_source_aware_without_word_limit() -> None:
    guard = _guard(
        semantic_over_inference_patterns=(
            "là biết", "chứng tỏ", "trong đầu cậu", "muốn tạo",
        ),
    )

    blocked = guard.assess(
        "Nhìn icon là biết cậu muốn tạo không khí vui vẻ.",
        grounding_text="vẫy tay với tui đi nữ hoàng :hugging_face:",
        enforce_semantic_grounding=True,
    )

    assert blocked.reasons == ("semantic_over_inference",)
    assert blocked.semantic_inference_pattern == "muốn tạo"
    assert guard.assess(
        "Cậu nói muốn tạo không khí vui vẻ.",
        grounding_text="tôi muốn tạo không khí vui vẻ",
        enforce_semantic_grounding=True,
    ).valid is True
    assert guard.assess(
        "Nhìn icon là biết cậu vui.", grounding_text=":)",
    ).valid is True
    assert guard.assess(
        "Cậu chắc chắn đang có âm mưu.", grounding_text="cậu vừa kể chuyện dài",
    ).valid is True


def test_question_clamp_keeps_only_existing_statement() -> None:
    guard = _guard(max_questions=0)

    assert guard.clamp_questions(
        "Phần này ổn rồi. Cậu nghĩ sao?",
    ) == "Phần này ổn rồi."
    assert guard.clamp_questions("Cậu nghĩ sao?") == "Cậu nghĩ sao?"


def test_human_like_prompt_contract_is_literal_and_correction_is_bounded() -> None:
    grounding = literal_grounding_directive()
    constraint = speech_style_constraint_prompt(
        (), avoid_question=False, max_sentences=2, max_words=65,
        forbidden_phrases=("rồi đấy",), require_vietnamese_integrity=True,
    )
    correction = speech_style_correction_prompt(
        grounding, "Nhưng kalau là tớ thì vui rồi đấy.",
        reasons=(
            "formula_phrase_budget", "language_integrity", "vague_grounding",
            "malformed_token", "semantic_over_inference",
        ),
        opener=None, phrase="rồi đấy", language_fragment="kalau",
        grounding_pattern="ý đồ", malformed_token="nghClient",
        semantic_inference_pattern="là biết",
        max_sentences=2, max_words=65,
    )

    assert "Keep hypotheticals conditional" in grounding
    assert "Never invent viewer intent" in grounding
    assert constraint is not None and "rồi đấy" in constraint
    assert "Dùng tiếng Việt tự nhiên" in constraint
    assert "kalau" in correction and "không thêm ý" in correction
    assert "ý đồ" in correction and "không hỏi thêm" in correction
    assert "nghClient" in correction and "là biết" in correction
