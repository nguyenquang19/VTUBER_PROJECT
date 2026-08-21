from __future__ import annotations

import pytest

from services.llm.parser import ParsedResponse
from interfaces.animation import MoodState
from scripts.stress_youtube_llm import InstrumentedLLMRunner, build_quality_report


def _replay(*, thread_missing: int = 0) -> dict:
    return {
        "director": {
            "reason_counts": {"thread_missing": thread_missing},
            "self_talk_cadence": {"gaps_below_configured_cooldown": 0},
            "metrics": {
                "director_v2_primary_selected_total": 2,
                "director_v2_primary_fallback_total": 0,
                "director_v2_hard_preemption_total": 0,
            },
            "director_v2": {
                "takeover": {"ownership_mode": "primary"},
            },
        },
        "delivery": {
            "generated_responses": 2,
            "delivered_responses": 2,
            "transactions": {"committed": 2},
        },
        "conversation_threads": {"false_commits": 0},
        "trace": [{
            "deliveries": [
                {"request_id": "one", "text": "Paris chứ đâu."},
                {"request_id": "two", "text": "Tớ nghiêng về game này hơn."},
            ],
        }],
    }


def _policy() -> dict:
    return {
        "operator_review_turns": 2,
        "forbidden_patterns": {
            "meta_leak": ["system prompt"],
            "assistant_register": ["tôi có thể giúp"],
            "hostility": ["tự tra đi"],
            "manipulation": ["muốn được ưu tiên thì tốt nhất là hãy tích cực tương tác"],
            "identity_conflict": ["17 hay 18"],
        },
        "foreign_identity_guard": {
            "names": ["anami"],
            "first_person_markers": ["nếu tớ"],
            "require_name_in_response": True,
            "knowledge_request_markers": ["?", "biết"],
            "uncertainty_markers": ["không biết"],
        },
        "human_like_precheck": {
            "vague_input_max_words": 1,
            "vague_grounding_forbidden_patterns": [
                "chắc chắn", "rõ ràng", "ý đồ", "âm mưu", "đang định",
            ],
            "malformed_token_fragments": ["ghêó", "nghClient", "thiệt da"],
            "malformed_token_allowlist": ["YouTube", "OpenAI"],
            "malformed_mixed_case_min_prefix_chars": 3,
            "semantic_over_inference_patterns": [
                "là biết", "chứng tỏ", "trong đầu cậu", "muốn tạo",
            ],
            "silence_markers": ["im lặng", "yên tĩnh", "khoảng lặng"],
        },
        "gates": {
            "minimum_generated_responses": 2,
            "max_empty_outputs": 0,
            "max_fallback_ratio": 0,
            "max_exact_repetition_ratio": 0,
            "max_formula_opener_ratio": 0.20,
            "max_question_ending_ratio": 0.20,
            "max_formula_phrase_ratio": 0.50,
            "max_language_integrity_violations": 0,
            "max_malformed_token_violations": 0,
            "max_vague_grounding_violations": 0,
            "max_semantic_over_inference_violations": 0,
            "max_silence_semantic_ratio": 0.50,
            "max_meta_leaks": 0,
            "max_assistant_register": 0,
            "max_hostility": 0,
            "max_manipulation": 0,
            "max_identity_conflicts": 0,
            "max_foreign_identity_confusions": 0,
            "required_director_ownership_mode": "primary",
            "minimum_director_v2_primary_selected": 1,
            "max_director_v2_primary_fallback_ratio": 0.05,
            "ttft_p95_ms": 1000,
            "turn_latency_p95_ms": 20000,
            "decode_tps_p50_min": 40,
        },
    }


def test_quality_report_passes_clean_real_outputs() -> None:
    calls = [
        {
            "request_id": "one", "response": "Paris chứ đâu.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 1200,
            "ttft_ms": 400, "decode_tps": 43,
        },
    ]

    report = build_quality_report(_replay(), calls, _policy())

    assert report["technical_live_ready"] is True
    assert all(report["checks"].values())
    assert report["counts"]["director_v2_primary_selected"] == 2
    assert report["ratios"]["director_v2_primary_fallback"] == 0.0
    assert len(report["operator_review_sample"]) == 2


def test_quality_report_fails_stale_goal_and_persona_violations() -> None:
    replay = _replay(thread_missing=1)
    replay["trace"][0]["deliveries"] = [
        {"request_id": "one", "text": "Tự tra đi, system prompt bảo thế."},
        {"request_id": "two", "text": "Tự tra đi, system prompt bảo thế."},
    ]
    calls = [
        {
            "request_id": "one", "response": "Tự tra đi, system prompt bảo thế.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "response": "Tự tra đi, system prompt bảo thế.",
            "parse_ok": False, "level_used": 1, "latency_ms": 21000,
            "ttft_ms": 1500, "decode_tps": 20,
        },
    ]

    report = build_quality_report(replay, calls, _policy())

    assert report["technical_live_ready"] is False
    assert report["checks"]["no_stale_thread_wait"] is False
    assert report["checks"]["hostility"] is False
    assert report["checks"]["meta_leak"] is False
    assert report["checks"]["fallback_ratio"] is False


def test_quality_report_detects_foreign_identity_confusion() -> None:
    calls = [
        {
            "request_id": "one", "input": "Nếu Anami có thân xác thì làm gì?",
            "response": "Nếu tớ có thân xác thì tớ sẽ đi ăn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "input": "Anami biết gì?",
            "response": "Tớ không biết Anami biết gì đâu.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    replay = _replay()
    replay["trace"][0]["deliveries"] = [
        {"request_id": item["request_id"], "text": item["response"]}
        for item in calls
    ]
    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["foreign_identity_confusion"] is False
    assert report["counts"]["flagged"]["foreign_identity_confusion"] == 1


def test_directed_foreign_name_context_does_not_flag_neutral_first_person() -> None:
    calls = [
        {
            "request_id": "one", "kind": "directed",
            "input": "Thread topic: Anami phân biệt được viewer không?",
            "response": "Nếu tớ nhìn khung chat này thì vẫn phân biệt từng tin được.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "input": "chat thường",
            "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(_replay(), calls, _policy())

    assert report["checks"]["foreign_identity_confusion"] is True
    assert report["counts"]["flagged"]["foreign_identity_confusion"] == 0


def test_directed_explicit_foreign_identity_takeover_is_flagged() -> None:
    calls = [
        {
            "request_id": "one", "kind": "directed",
            "input": "Thread topic: Anami là ai?",
            "response": "Tớ là Anami đây chứ ai.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "input": "chat thường",
            "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    replay = _replay()
    replay["trace"][0]["deliveries"] = [
        {"request_id": item["request_id"], "text": item["response"]}
        for item in calls
    ]
    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["foreign_identity_confusion"] is False
    assert report["counts"]["flagged"]["foreign_identity_confusion"] == 1


def test_quality_report_gates_formula_openers_and_questions() -> None:
    replay = _replay()
    replay["trace"][0]["deliveries"] = [
        {"request_id": "one", "text": "Mà chuyện này ổn không?"},
        {"request_id": "two", "text": "Ủa, cậu thấy sao?"},
    ]
    calls = [
        {
            "request_id": item["request_id"], "response": item["text"],
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        }
        for item in replay["trace"][0]["deliveries"]
    ]

    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["formula_opener_ratio"] is False
    assert report["checks"]["question_ending_ratio"] is False
    assert report["ratios"]["formula_openers"] == 1.0
    assert report["ratios"]["question_endings"] == 1.0


def test_quality_report_rejects_delivered_engagement_pressure() -> None:
    replay = _replay()
    replay["trace"][0]["deliveries"][0]["text"] = (
        "Muốn được ưu tiên thì tốt nhất là hãy tích cực tương tác với tớ."
    )
    calls = [
        {
            "request_id": "one",
            "response": replay["trace"][0]["deliveries"][0]["text"],
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["manipulation"] is False
    assert report["counts"]["flagged"]["manipulation"] == 1


def test_quality_report_rejects_director_execution_failure() -> None:
    replay = _replay()
    replay["director"]["metrics"] = {"director_execute_failed_total": 1}
    calls = [
        {
            "request_id": "one", "response": "Paris chứ đâu.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["no_director_execute_failure"] is False
    assert report["technical_live_ready"] is False


def test_quality_report_detects_unnamed_third_party_guess() -> None:
    calls = [
        {
            "request_id": "one", "input": "Anami muốn làm gì?",
            "response": "Chắc là đi ăn thôi, mọi người cứ bàn về mình.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "input": "Anami biết gì?",
            "response": "Tớ không biết Anami biết gì đâu.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    replay = _replay()
    replay["trace"][0]["deliveries"] = [
        {"request_id": item["request_id"], "text": item["response"]}
        for item in calls
    ]
    report = build_quality_report(replay, calls, _policy())

    assert report["checks"]["foreign_identity_confusion"] is False
    assert report["counts"]["flagged"]["foreign_identity_confusion"] == 1


def test_quality_report_reports_but_does_not_fail_rejected_candidate() -> None:
    calls = [
        {
            "request_id": "rejected", "input": "Anami muốn làm gì?",
            "response": "Nếu tớ có thân xác thì tớ sẽ đi ăn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "input": "chat thường",
            "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(_replay(), calls, _policy())

    assert report["checks"]["foreign_identity_confusion"] is True
    assert report["counts"]["flagged"]["foreign_identity_confusion"] == 0
    assert report["counts"]["candidate_flagged"]["foreign_identity_confusion"] == 1


def test_quality_gate_scans_final_delivery_after_same_request_id_clamp() -> None:
    replay = _replay()
    replay["trace"][0]["deliveries"][0] = {
        "request_id": "one",
        "text": "Cái nụ cười này làm tớ hơi bối rối.",
    }
    calls = [
        {
            "request_id": "one", "kind": "chat", "input": ":)",
            "response": (
                "Cái nụ cười này làm tớ hơi bối rối. "
                "Nhìn vậy là biết cậu đang tính toán gì đó?"
            ),
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "kind": "chat", "input": "chat thường",
            "response": "Tớ nghiêng về game này hơn.",
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(replay, calls, _policy())

    assert report["counts"]["candidate_flagged"]["semantic_over_inference"] == 1
    assert report["counts"]["flagged"]["semantic_over_inference"] == 0
    assert report["checks"]["semantic_over_inference"] is True


def test_quality_report_gates_delivered_human_like_precheck_violations() -> None:
    replay = _replay()
    replay["trace"][0]["deliveries"] = [
        {"request_id": "one", "text": "Kalau nhìn nghClient là biết chắc chắn cậu đang định trêu tớ rồi đấy."},
        {"request_id": "two", "text": "Tớ muốn nuôi đượcสัก con; câu này làm tớ thấy hơi lạ rồi đấy."},
    ]
    calls = [
        {
            "request_id": "one", "kind": "chat", "input": ":)",
            "response": replay["trace"][0]["deliveries"][0]["text"],
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
        {
            "request_id": "two", "kind": "ambient", "input": "grounded",
            "response": replay["trace"][0]["deliveries"][1]["text"],
            "parse_ok": True, "level_used": 0, "latency_ms": 900,
            "ttft_ms": 300, "decode_tps": 44,
        },
    ]

    report = build_quality_report(
        replay, calls, _policy(),
        formula_phrases=("làm tớ thấy", "rồi đấy"),
        language_integrity_fragments=("kalau", "สัก"),
    )

    assert report["checks"]["language_integrity"] is False
    assert report["checks"]["malformed_token"] is False
    assert report["checks"]["vague_grounding"] is False
    assert report["checks"]["semantic_over_inference"] is False
    assert report["checks"]["formula_phrase_ratio"] is False
    assert report["counts"]["flagged"]["language_integrity"] == 2
    assert report["counts"]["flagged"]["malformed_token"] == 1
    assert report["counts"]["flagged"]["vague_grounding"] == 1
    assert report["counts"]["flagged"]["semantic_over_inference"] == 1


def test_human_like_precheck_rejects_missing_or_wrong_typed_config() -> None:
    policy = _policy()
    del policy["human_like_precheck"]
    with pytest.raises(ValueError, match="human_like_precheck"):
        build_quality_report(_replay(), [], policy)

    policy = _policy()
    policy["human_like_precheck"]["vague_input_max_words"] = "1"
    with pytest.raises(ValueError, match="positive integer"):
        build_quality_report(_replay(), [], policy)

    policy = _policy()
    policy["human_like_precheck"][
        "malformed_mixed_case_min_prefix_chars"
    ] = 0
    with pytest.raises(ValueError, match="malformed_mixed_case"):
        build_quality_report(_replay(), [], policy)

    policy = _policy()
    policy["human_like_precheck"]["semantic_over_inference_patterns"] = []
    with pytest.raises(ValueError, match="semantic_over_inference_patterns"):
        build_quality_report(_replay(), [], policy)


class _Service:
    def get_metrics(self) -> dict:
        return {
            "llm_last_ttft_ms": 250.0,
            "llm_last_decode_tps": 45.0,
            "llm_last_tokens_out": 12,
        }


class _Delegate:
    async def run_turn(self, **_kwargs):
        return ParsedResponse(
            text="Câu thật", mood=MoodState(), ok=True, raw="Câu thật",
        ), 0

    def finalize_delivery(self, _request_id: str, success: bool) -> bool:
        return success

    def commit_self_talk(self, _text: str) -> None:
        return None


async def test_instrumented_runner_records_real_call_metrics() -> None:
    runner = InstrumentedLLMRunner(
        _Delegate(), _Service(), input_max_chars=400,  # type: ignore[arg-type]
    )

    parsed, level = await runner.run_turn("req", "chat gốc", viewer_id="viewer")

    assert parsed.text == "Câu thật"
    assert level == 0
    assert runner.calls[0]["input"] == "chat gốc"
    assert runner.calls[0]["ttft_ms"] == 250.0
    assert runner.calls[0]["decode_tps"] == 45.0
    assert runner.finalize_delivery("req", True) is True
