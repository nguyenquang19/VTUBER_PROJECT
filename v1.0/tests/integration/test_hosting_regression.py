from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_hosting_session import RUBRIC, build_review_sheet
from services.data.sanitize import mask_pii

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "hosting_session_sanitized.json"
BASELINE = ROOT / "docs" / "baselines" / "m5_hosting_eval.json"


def test_sanitized_hosting_fixture_meets_review_contract() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    sheet = build_review_sheet(
        fixture["records"], turn_count=baseline["review_turn_count"],
        source_label="sanitized fixture",
    )
    assert baseline["operator_review"]["status"] == "template_ready_pending_live_review"
    assert sheet["turn_count"] == 20
    assert set(sheet["rubric"]) == set(baseline["rubric_dimensions"]) == set(RUBRIC)
    assert sheet["automated_metrics"]["mood_exposition_count"] <= (
        baseline["automated"]["mood_exposition_count_max"]
    )
    rendered = json.dumps(sheet, ensure_ascii=False)
    assert "viewer_id" not in rendered
    assert "session_id" not in rendered
    for record in fixture["records"]:
        assert mask_pii(record["user_text"]) == record["user_text"]
        assert mask_pii(record["mai_text"]) == record["mai_text"]


def test_review_template_exposes_anti_confabulation_and_hostness_explicitly() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sheet = build_review_sheet(fixture["records"], turn_count=20)
    assert "visible evidence" in sheet["rubric"]["non_confabulation"]
    assert "goals/threads" in sheet["rubric"]["hostness"]
    assert sheet["operator_review"]["scores"]["non_confabulation"] is None
