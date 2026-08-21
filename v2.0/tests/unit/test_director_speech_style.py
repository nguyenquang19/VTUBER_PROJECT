from services.director.speech_style import (
    SpeechStyleGuard,
    looks_like_question,
    summarize_speech_style,
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


def test_question_clamp_keeps_only_existing_declarative_sentences() -> None:
    guard = _guard()
    text = "Sao lại gọi sai tên thế? Tớ là Mai cơ mà, nhớ kỹ vào đấy."

    assert guard.clamp_excess_questions(text) == "Tớ là Mai cơ mà, nhớ kỹ vào đấy."
    assert guard.clamp_excess_questions("Cười gì hả?") == "Cười gì hả?"
