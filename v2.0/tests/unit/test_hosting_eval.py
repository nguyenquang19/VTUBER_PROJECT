from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.eval_hosting_session import (
    RUBRIC, build_review_sheet, iter_jsonl, validate_operator_review,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "hosting_session_sanitized.json"


def _records() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]


def _completed_sheet() -> dict:
    sheet = build_review_sheet(_records(), turn_count=20, source_label="test")
    sheet["operator_review"] = {
        "reviewer": "operator",
        "scores": {key: 4 for key in RUBRIC},
        "notes": {key: f"evidence note for {key}" for key in RUBRIC},
        "flagged_turns": [7, 11],
    }
    return sheet


def test_builds_exact_six_dimension_review_for_twenty_turns() -> None:
    sheet = build_review_sheet(_records(), turn_count=20, source_label="fixture")
    assert sheet["turn_count"] == 20
    assert set(sheet["rubric"]) == set(RUBRIC)
    assert all(value is None for value in sheet["operator_review"]["scores"].values())
    assert sheet["raw_transcript_committed"] is False


@pytest.mark.parametrize("turn_count", [19, 31])
def test_rejects_review_outside_twenty_to_thirty_turns(turn_count: int) -> None:
    with pytest.raises(ValueError, match="20-30"):
        build_review_sheet(_records() * 2, turn_count=turn_count)


def test_sanitizes_direct_identifiers_and_never_exports_structured_ids() -> None:
    records = [
        {
            "kind": "chat_reply",
            "viewer_id": "raw-secret-id",
            "viewer_name": "Alice",
            "user_text": "Alice email alice@example.com",
            "mai_text": "Chào Alice",
        },
    ] * 20
    sheet = build_review_sheet(records, turn_count=20)
    rendered = json.dumps(sheet, ensure_ascii=False)
    assert "raw-secret-id" not in rendered
    assert "alice@example.com" not in rendered
    assert "Alice" not in rendered
    assert "[PII]" in rendered


def test_completed_operator_review_requires_scores_and_notes() -> None:
    result = validate_operator_review(_completed_sheet())
    assert result["complete"] is True
    assert result["average_score"] == 4.0
    broken = _completed_sheet()
    broken["operator_review"]["notes"]["hostness"] = ""
    with pytest.raises(ValueError, match="hostness"):
        validate_operator_review(broken)


def test_boolean_or_out_of_range_score_is_rejected() -> None:
    for invalid in (True, 0, 6, 3.5):
        sheet = _completed_sheet()
        sheet["operator_review"]["scores"]["persona"] = invalid
        with pytest.raises(ValueError, match="persona"):
            validate_operator_review(sheet)


def test_automated_metrics_measure_repetition_and_proactive_ratio() -> None:
    sheet = build_review_sheet(_records(), turn_count=20)
    metrics = sheet["automated_metrics"]
    assert 0 <= metrics["opener_repeat_ratio"] <= 1
    assert metrics["proactive_turn_ratio"] == 0.4
    assert metrics["mood_exposition_count"] == 0


def test_iter_jsonl_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    path.write_text('{"kind":"ambient","mai_text":"ok"}\nnot-json\n', encoding="utf-8")
    assert list(iter_jsonl(path)) == [{"kind": "ambient", "mai_text": "ok"}]
