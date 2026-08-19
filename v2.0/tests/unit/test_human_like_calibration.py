from __future__ import annotations

from datetime import datetime, timezone

import pytest

from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from services.evaluation.human_like import (
    HumanLikeCalibration, HumanLikeCalibrationConfig, TrajectoryRecorder,
)


CONFIG = HumanLikeCalibrationConfig(
    max_artifact_rows=4, max_candidate_chars=100, max_note_chars=100,
    max_smell_tags=3, max_trajectory_records=2, max_reason_codes=2,
    weights={"language": .2, "presence": .25, "context": .15, "character": .15, "timing": .15, "spontaneity": .1},
)


def _review(*, smell: bool = False) -> dict:
    return {
        "language": 4, "presence": 5, "context": 3, "character": 4,
        "timing": 3, "spontaneity": 2, "ai_smell": smell,
        "ai_smell_tags": ["template"] if smell else [], "liveness": 4,
        "action_coherence": 5, "note": "human checked the behavior",
    }


def _request() -> ActionRequest:
    return ActionRequest(
        schema_version=1, action_id="a1", capability_id="WAIT", action_type="WAIT",
        target=None, arguments={"text": "must not be retained"}, intention_id=None,
        evidence_refs=("event-1",), idempotency_key="k1", priority=0,
        requested_at=datetime(2026, 8, 15, tzinfo=timezone.utc), transaction_policy="none",
    )


def test_blind_artifact_hides_internals_until_scores_are_finalized() -> None:
    service = HumanLikeCalibration(CONFIG, enabled=True)
    artifact = service.build(({
        "turn_ref": "turn-1", "candidate": "Mai answers naturally", "build_label": "v2-candidate",
        "director_score": 99.0, "trajectory_ref": "traj-1", "prompt": "secret", "memory": "secret",
    },))
    rendered = str(artifact)
    assert "v2-candidate" not in rendered and "99.0" not in rendered
    assert "secret" not in rendered
    with pytest.raises(ValueError, match="only after"):
        service.reveal_internals(artifact)
    artifact["human_review"] = {"reviewer_role": "operator"}
    artifact["rows"][0]["review"] = _review(smell=True)
    final = service.finalize(artifact)
    assert final["human_review"]["aggregate"] == 3.75
    assert final["human_review"]["weakest_dimension"] == "spontaneity"
    assert final["human_review"]["ai_smell_ratio"] == 1.0
    assert service.reveal_internals(final)["rows"][final["rows"][0]["review_ref"]]["build_label"] == "v2-candidate"


def test_hlc_delta_and_smell_tag_validation_are_explicit() -> None:
    service = HumanLikeCalibration(CONFIG, enabled=True)
    artifact = service.build(({"candidate": "ok"},))
    artifact["previous_build_score"] = 3.0
    artifact["human_review"] = {"reviewer_role": "operator"}
    artifact["rows"][0]["review"] = _review()
    final = service.finalize(artifact)
    assert final["human_review"]["previous_build_delta"] == 0.75
    artifact = service.build(({"candidate": "ok"},))
    artifact["human_review"] = {"reviewer_role": "operator"}
    artifact["rows"][0]["review"] = {**_review(smell=True), "ai_smell_tags": []}
    with pytest.raises(ValueError, match="requires"):
        service.finalize(artifact)


def test_trajectory_is_bounded_structured_and_preserves_failed_result() -> None:
    recorder = TrajectoryRecorder(max_recent=2, max_reason_codes=2, enabled=True, clock=lambda: 1.0)
    trajectory_id = recorder.record_decision(
        start_snapshots={"world_snapshot_id": "world-1", "self_snapshot_id": "self-1", "capability_snapshot_id": "cap-1"},
        candidate_summary={"candidate_count": 2, "candidate_kinds": ("chat",), "top_score": 4.0},
        selected_action="WAIT", reason_codes=("no_chat", "idle", "extra"), action_request=_request(),
    )
    assert trajectory_id is not None
    result = ActionResult(
        schema_version=1, action_id="a1", status=ActionStatus.FAILED,
        started_at=datetime(2026, 8, 15, tzinfo=timezone.utc), completed_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        verified=False, verification_source=None, result_data={"secret": "not retained"}, error_code="unavailable",
    )
    assert recorder.update_result(trajectory_id, result=result, verification_outcome="not_verified", next_snapshots={"world_snapshot_id": "world-2"})
    row = recorder.snapshot()["recent"][0]
    assert row["result"]["status"] == "failed" and row["result"]["verified"] is False
    assert "must not be retained" not in str(row) and "not retained" not in str(row)
    assert recorder.record_decision(start_snapshots={"prompt": "secret"}, candidate_summary={}, selected_action="WAIT") is None

def test_paired_blind_review_hides_build_mapping_until_finalize() -> None:
    service = HumanLikeCalibration(CONFIG, enabled=True)
    artifact = service.build(({
        "turn_ref": "pair-1", "candidate_a": "legacy answer", "candidate_b": "candidate answer",
        "build_label": "candidate-build",
    },))
    row = artifact["rows"][0]
    assert row["paired_blind_review"] is True
    assert set((row["candidate_a"], row["candidate_b"])) == {"legacy answer", "candidate answer"}
    artifact["human_review"] = {"reviewer_role": "operator"}
    artifact["rows"][0]["review"] = {**_review(), "preferred": "A"}
    assert service.finalize(artifact)["status"] == "finalized"