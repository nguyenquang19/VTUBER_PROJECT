"""Sanitized live-evaluation artifacts and explicit human review (M8.2)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from services.data.sanitize import mask_pii
from services.evaluation.types import EvalOutcome, ScenarioResult, ScenarioSuite


def build_live_artifact(
    suite: ScenarioSuite,
    results: tuple[ScenarioResult, ...],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Build a shareable marker artifact; raw prompts/transcripts are never copied."""
    if not run_id.strip():
        raise ValueError("live evaluation run id is required")
    by_id = {scenario.scenario_id: scenario for scenario in suite.scenarios}
    rows: list[dict[str, Any]] = []
    for result in results:
        scenario = by_id[result.scenario_id]
        rows.append({
            **result.to_dict(),
            "source_refs": [_evidence_ref(item) for item in result.source_refs],
            "human_rubric": [
                {
                    "dimension": item.dimension,
                    "instruction": item.instruction,
                    "required": item.required,
                    "score": None,
                    "note": "",
                }
                for item in scenario.human_rubric
            ],
        })
    status = _artifact_status(rows, review_complete=False)
    return {
        "schema_version": 1,
        "contract_id": suite.contract_id,
        "run_id": _compact(run_id, 80),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "marker": "live_eval",
        "sanitized": True,
        "raw_model_output_included": False,
        "status": status,
        "results": rows,
        "human_review": {"reviewer_role": "", "complete": False},
    }


def finalize_human_review(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Validate operator-entered 1-5 scores; automated checks cannot fill them."""
    output = dict(artifact)
    if output.get("marker") != "live_eval" or output.get("sanitized") is not True:
        raise ValueError("review requires a sanitized live_eval artifact")
    review = dict(output.get("human_review") or {})
    reviewer_role = _compact(str(review.get("reviewer_role") or ""), 40)
    if not reviewer_role:
        raise ValueError("human reviewer role is required")
    rows = [dict(item) for item in output.get("results") or []]
    if not rows:
        raise ValueError("evaluation artifact has no results")
    reviewed = 0
    for row in rows:
        rubrics = [dict(item) for item in row.get("human_rubric") or []]
        for rubric in rubrics:
            if not rubric.get("required", True):
                continue
            score = rubric.get("score")
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                raise ValueError(
                    f"rubric score {rubric.get('dimension')} must be an integer within [1, 5]"
                )
            note = _compact(mask_pii(str(rubric.get("note") or "")) or "", 400)
            if not note:
                raise ValueError(f"rubric note {rubric.get('dimension')} is required")
            rubric["note"] = note
            reviewed += 1
        row["human_rubric"] = rubrics
    output["results"] = rows
    output["human_review"] = {
        "reviewer_role": reviewer_role,
        "complete": True,
        "reviewed_rubrics": reviewed,
    }
    output["status"] = _artifact_status(rows, review_complete=True)
    return output


def _artifact_status(rows: list[dict[str, Any]], *, review_complete: bool) -> str:
    outcomes = {str(row.get("outcome")) for row in rows}
    if EvalOutcome.FAILED.value in outcomes:
        return "failed"
    if EvalOutcome.NOT_OBSERVED.value in outcomes:
        return "not_observed"
    requires_human = any(
        any(rubric.get("required", True) for rubric in row.get("human_rubric") or [])
        for row in rows
    )
    if requires_human and not review_complete:
        return "pending_human_review"
    return "passed"


def _evidence_ref(value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"evidence:{digest}"


def _compact(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]
